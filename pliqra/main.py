from .config import env,http_verify
import asyncio
import csv
import io
import json
import os
import re
import secrets
import sqlite3
import psycopg
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx
from authlib.integrations.starlette_client import OAuth
from fastapi import Depends, FastAPI, HTTPException, Request, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StringConstraints, ValidationError, field_validator
from starlette.middleware.sessions import SessionMiddleware
from starlette._utils import get_route_path
from itsdangerous import TimestampSigner

from . import __version__
from .access import service_allowed, project_ids, require_project, sync_group_memberships, current_principal
from .connectors import discover, response_json
from .db import Store, encode, now, uid
from .gateway import CAPABILITIES, Gateway, decoded_deployment, decoded_port
from .security import Vault, digest, endpoint_url, hash_password, verify_password, bearer_token
from .inference_schema import ChatInput
from .protocol import PROTOCOL_VERSION, ProtocolVersionMiddleware, version_metadata

STATIC = Path(__file__).parent / "static"
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class WorkspaceSessionMiddleware(SessionMiddleware):
    def __init__(self,app,key_provider,**kwargs):
        self.key_provider=key_provider
        super().__init__(app,secret_key=key_provider(),**kwargs)

    async def __call__(self,scope,receive,send):
        if scope.get('type')=='http' and get_route_path(scope) in ('/livez','/readyz','/healthz'):
            return await self.app(scope,receive,send)
        self.signer=TimestampSigner(self.key_provider())
        return await super().__call__(scope,receive,send)


class Login(BaseModel):
    email: Annotated[str, Field(min_length=3, max_length=254)]
    password: Annotated[str, Field(min_length=1, max_length=512)]


class Setup(Login):
    name: Name

    @field_validator("password")
    @classmethod
    def strong_password(cls, value):
        if len(value) < 12:
            raise ValueError("Use at least 12 characters")
        return value


from .topology import RuntimeTopology,Placement,SiteID


class ConnectionInput(BaseModel):
    site:SiteID|Literal['']|None=None
    region:SiteID|Literal['']|None=None
    name: Name
    endpoint: Annotated[str, Field(min_length=8, max_length=2000)]
    api_key: Annotated[str, Field(max_length=4096)] = ""
    provider: Literal["auto", "openai", "ollama", "responses", "anthropic"] = "auto"
    model: Annotated[str, Field(max_length=256)] = ""
    auth_header: Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9-]{0,63}$")] = "Authorization"
    auth_prefix: Annotated[str, Field(max_length=32, pattern=r"^[A-Za-z0-9 -]*$")] = "Bearer"


class DeploymentInput(BaseModel):
    capabilities: list[str]
    input_price: Annotated[float | None, Field(ge=0, le=1000000)] = None
    output_price: Annotated[float | None, Field(ge=0, le=1000000)] = None
    enabled: bool = True

    @field_validator("capabilities")
    @classmethod
    def valid_caps(cls, values):
        if set(values) - CAPABILITIES:
            raise ValueError("Unknown capability")
        return sorted(set(values))


class PortInput(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._/-]*$")]
    description: Annotated[str, Field(max_length=1000)] = ""
    deployments: Annotated[list[str], Field(min_length=1, max_length=100)]
    requirements: list[str] = []
    project_id: str | None = None
    verified_only: bool = False
    log_content: bool = False
    cache_ttl: Annotated[int, Field(ge=0, le=3600)] = 0
    max_input_chars: Annotated[int, Field(ge=1, le=1000000)] = 200000
    max_output_tokens: Annotated[int, Field(ge=1, le=131072)] = 32768
    placement:Placement=Placement()
    strategy: Literal["priority", "round_robin"] = "priority"
    enabled: bool = True

    @field_validator("requirements")
    @classmethod
    def valid_caps(cls, values):
        if set(values) - CAPABILITIES:
            raise ValueError("Unknown capability")
        return sorted(set(values))


class ProjectInput(BaseModel):
    name: Name
    monthly_budget: Annotated[float | None, Field(ge=0, le=10000000)] = None
    max_concurrency: Annotated[int, Field(ge=1, le=200)] = 20


class KeyInput(BaseModel):
    name: Name
    project_id: str = "default"
    team_id: str | None = None
    gateway_id: str | None = None
    allowed_ports: list[str] = []
    rpm: Annotated[int, Field(ge=1, le=100000)] = 60
    expires_days: Annotated[int | None, Field(ge=1, le=365)] = 90


class IdentityInput(BaseModel):
    name: Name
    issuer: Annotated[str, Field(min_length=8, max_length=2000)]
    client_id: Annotated[str, Field(min_length=1, max_length=512)]
    client_secret: Annotated[str, Field(min_length=1, max_length=4096)]
    default_role: Literal["viewer"] = "viewer"


class UserInput(Setup):
    role: Literal["admin", "viewer"] = "viewer"
    project_ids: list[str] = []


class UserUpdate(BaseModel):
    role: Literal["admin", "viewer"]
    active: bool


class BindingInput(BaseModel):
    service: str
    gateway_id: str | None = None
    requirements: list[str] = []
    required_extensions:Annotated[list[str],Field(max_length=20)]=[]
    optional_extensions:Annotated[list[str],Field(max_length=20)]=[]
    ttl_seconds: Annotated[int, Field(ge=60, le=86400)] = 3600

    @field_validator("requirements")
    @classmethod
    def valid_caps(cls, values):
        if set(values) - CAPABILITIES:
            raise ValueError("Unknown capability")
        return sorted(set(values))


def safe_user(row):
    return {key: row[key] for key in ("id", "email", "name", "role", "issuer", "active", "created")}


def create_app(data_dir=None, *, database_url=None, database_schema=None, encryption_key=None, organization=None, public_url_override=None, runtime_topology=None):
    directory = Path(data_dir or env("PLIQRA_DATA_DIR", "data"))
    runtime_topology=runtime_topology or RuntimeTopology.from_env()
    selected_url=database_url if database_url is not None else env('PLIQRA_DATABASE_URL','')
    if runtime_topology.multi_dc:
        if not selected_url:raise RuntimeError('Multi-DC mode requires one shared writable PostgreSQL authority')
        options=psycopg.conninfo.conninfo_to_dict(selected_url)
        addresses=options.get('host') or options.get('hostaddr')
        if not addresses:raise RuntimeError('Multi-DC database addresses must be explicit')
        hosts=addresses.split(',')
        if any(h not in ('localhost','127.0.0.1','::1') for h in hosts) and options.get('sslmode')!='verify-full':raise RuntimeError('Remote multi-DC control databases require sslmode=verify-full')
    store, login_attempts = Store(directory,database_url,database_schema), {}
    try:
        store.runtime_topology=runtime_topology
        vault = Vault(directory,store,encryption_key)
    except BaseException:
        store.close()
        raise
    oauth = OAuth()
    secure_cookies = env("PLIQRA_SECURE_COOKIES", "0") == "1"
    public_url = (public_url_override if public_url_override is not None else env("PLIQRA_PUBLIC_URL", "")).rstrip("/")
    base_path='/org/'+organization['slug'] if organization else ''
    cookie_name='plq_session_'+organization['id'] if organization else 'plq_session'

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=5), follow_redirects=False, limits=httpx.Limits(max_connections=200, max_keepalive_connections=40), trust_env=False,verify=http_verify(),transport=getattr(app.state,'upstream_transport',None)) as client:
            app.state.loop = asyncio.get_running_loop()
            app.state.client = client
            app.state.gateway = Gateway(store, vault, client)
            from .operations import maintain
            worker = asyncio.create_task(maintain(app))
            heartbeat = asyncio.create_task(app.state.gateway.cluster.heartbeat())
            from .cluster import restart_when_fenced
            restart_task=asyncio.create_task(restart_when_fenced(app.state.gateway.cluster)) if env('PLIQRA_RESTART_ON_FENCE','0')=='1' else None
            app.state.jobs = set()
            from .batches import worker as batch_worker
            batch_worker_task=asyncio.create_task(batch_worker(app))
            from .platform import analytics_worker
            analytics_task=asyncio.create_task(analytics_worker(app))
            from .fleet import worker as fleet_worker
            fleet_task=asyncio.create_task(fleet_worker(app))
            try:
                yield
            finally:
                if hasattr(app.state,'organizations'):await app.state.organizations.close()
                worker.cancel()
                heartbeat.cancel()
                if restart_task:restart_task.cancel()
                batch_worker_task.cancel()
                analytics_task.cancel()
                fleet_task.cancel()
                for job in list(app.state.jobs):
                    job.cancel()
                await asyncio.gather(worker, heartbeat, batch_worker_task, analytics_task, fleet_task, *([restart_task] if restart_task else []), *list(app.state.jobs), return_exceptions=True)
                app.state.gateway.cluster.close()
        store.close()

    app = FastAPI(title="Pliqra", version=__version__, description="Endpoint-first model connectivity. Management API, OpenAI-compatible chat and Pliqra Connection Protocol 1.0 with legacy compatibility.", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.store, app.state.vault, app.state.oauth = store, vault, oauth
    app.add_middleware(WorkspaceSessionMiddleware, key_provider=lambda:vault.session_secret()+store.setting('auth_epoch',''), session_cookie='plq_oidc_'+organization['id'] if organization else 'plq_oidc', path=base_path or '/', same_site="lax", https_only=secure_cookies, max_age=600)

    @app.middleware("http")
    async def boundaries(request, call_next):
        path=get_route_path(request.scope)
        saml_acs = path.startswith("/auth/saml/") and path.endswith("/acs")
        if request.method in ("POST", "PATCH", "PUT", "DELETE"):
            origin = request.headers.get("origin")
            expected = public_url or str(request.base_url).rstrip("/")
            origin_parts=urlsplit(expected)
            expected=origin_parts.scheme+'://'+origin_parts.netloc
            if origin and origin != expected and not saml_acs:
                return JSONResponse({"detail": "Cross-origin writes are not permitted"}, 403)
            allowed_types = {"application/json", "application/scim+json"}
            audio_upload=path.endswith('/audio/transcriptions')
            if audio_upload:allowed_types.add('multipart/form-data')
            if saml_acs:
                allowed_types.add("application/x-www-form-urlencoded")
            if request.method != "DELETE" and request.headers.get("content-type", "").split(";")[0].strip().lower() not in allowed_types:
                return JSONResponse({"detail": "Use application/json"}, 415)
            try:
                content_length = int(request.headers.get("content-length", "0"))
            except ValueError:
                return JSONResponse({"detail": "Invalid content length"}, 400)
            body_limit = 96_000_000 if path == "/api/v1/backups/restore" else 44_100_000 if audio_upload or path.endswith('/operations/transcription') else 4_000_000
            if content_length > body_limit:
                return JSONResponse({"detail": "Request exceeds 4 MB"}, 413)
            # Enforce the limit for chunked requests as well as Content-Length.
            size, chunks = 0, []
            async for chunk in request.stream():
                size += len(chunk)
                if size > body_limit:
                    return JSONResponse({"detail": "Request exceeds 4 MB"}, 413)
                chunks.append(chunk)
            request._body = b"".join(chunks)
        if path=='/livez':return JSONResponse({'status':'alive'},headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
        control=(request.method in ('POST','PATCH','PUT','DELETE') and path.startswith(('/api/','/scim/'))) or path.startswith('/auth/') or path=='/protocol/v1/bindings'
        try:
            if control and path not in ('/api/v1/backups/restore','/api/chat','/api/generate'):
                with app.state.gateway.cluster.checking('management'):
                    response=await call_next(request)
            else:response = await call_next(request)
        except HTTPException as exc:return JSONResponse({'detail':exc.detail},exc.status_code,headers={'Cache-Control':'no-store',**(exc.headers or {})})
        except psycopg.OperationalError:
            app.state.gateway.cluster.fence()
            return JSONResponse({'detail':'Storage connection failed; restart this replica after database recovery. Inspect requests before retrying inference.'},503,headers={'Retry-After':'5','Cache-Control':'no-store'})
        response.headers['X-Pliqra-Site']=app.state.gateway.cluster.topology.site
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        if response.status_code==401:
            response.headers.setdefault('WWW-Authenticate','Bearer realm="pliqra"')
        if response.status_code==401 or path in ("/healthz","/readyz","/metrics") or path.startswith(("/api/", "/auth/", "/protocol/", "/v1/", "/g/", "/scim/")):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Never echo submitted passwords, provider secrets or prompts from validation errors.
        return JSONResponse({"detail": [{"field": ".".join(str(part) for part in item["loc"]), "message": item["msg"]} for item in exc.errors()]}, 422)

    @app.exception_handler(ValidationError)
    async def schema_error(request,exc):
        return JSONResponse({'detail':[{'field':'.'.join(map(str,item['loc'])),'message':item['msg']} for item in exc.errors()]},422)

    @app.exception_handler(json.JSONDecodeError)
    async def malformed_json(request,exc):
        return JSONResponse({'detail':'Malformed JSON document'},400)

    @app.exception_handler(sqlite3.IntegrityError)
    async def integrity_error(request, exc):
        return JSONResponse({"detail": "This configuration conflicts with an existing record. Refresh and try again."}, 409)

    @app.exception_handler(psycopg.OperationalError)
    async def database_unavailable(request,exc):
        return JSONResponse({'detail':'Database is temporarily unavailable. Inspect request records before retrying a model call.'},503,headers={'Retry-After':'5'})

    def principal(request: Request):
        authorization = request.headers.get("authorization", "")
        if not authorization and request.url.path.endswith('/v1/messages') and request.headers.get('x-api-key'):
            authorization='Bearer '+request.headers['x-api-key']
        if authorization:
            token=bearer_token(authorization)
            if token is None:raise HTTPException(401,'Use Bearer authentication')
            key = store.one("SELECT * FROM api_keys WHERE token_hash=? AND active=1", (digest(token),))
            if not key or (key["expires"] and key["expires"] <= now()):
                raise HTTPException(401, "Invalid or expired access key")
            return {"id": key["id"], "kind": "key", "role": "consumer", "key": key}
        token = request.cookies.get(cookie_name, "")
        row = store.one("SELECT s.csrf,s.expires,u.* FROM sessions s JOIN users u ON s.user_id=u.id WHERE s.token_hash=? AND u.active=1 AND (u.issuer='local' OR EXISTS(SELECT 1 FROM identity_providers i WHERE i.id=u.issuer AND i.active=1))", (digest(token),)) if token else None
        if not row or row["expires"] <= now():
            raise HTTPException(401, "Sign in to continue")
        if request.method in ("POST", "PATCH", "PUT", "DELETE") and not secrets.compare_digest(request.headers.get("x-pliqra-csrf", ""), row["csrf"]):
            raise HTTPException(403, "Session verification failed. Refresh and try again.")
        return {"id": row["id"], "kind": "user", "role": row["role"], "user": row, "csrf": row["csrf"],"session_hash":digest(token)}

    def user(p=Depends(principal)):
        if p["kind"] != "user":
            raise HTTPException(403, "A signed-in user is required for the management API")
        return p

    def admin(p=Depends(user)):
        if p["role"] != "admin":
            raise HTTPException(403, "Administrator access is required")
        return p

    def session_metadata():
        topology=store.runtime_topology
        label='Multi-DC · '+topology.site if topology.multi_dc else 'PostgreSQL · shared state' if store.backend=='postgresql' else 'Single container · SQLite'
        return {'workspace_name':(organization or {}).get('name','Your workspace'),'deployment_label':label,'version':__version__}

    def session_response(account, request=None, federation_context=None):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with store.transaction():
            fresh=current_principal(store,{'kind':'user','id':account['id']})['user']
            if account['issuer']=='local' and fresh['password_hash']!=account['password_hash']:
                raise HTTPException(401,'Credentials changed during sign-in; authenticate again')
            account=fresh
            store.run("DELETE FROM sessions WHERE expires<?", (now(),))
            store.insert("sessions", {"token_hash": digest(token), "user_id": account["id"], "csrf": csrf, "expires": now() + 43200,'federation_context':encode(federation_context or {})})
        response = RedirectResponse(base_path+'/', 303) if request else JSONResponse({"user": safe_user(account), "csrf": csrf,**session_metadata()})
        response.set_cookie(cookie_name, token, httponly=True, secure=secure_cookies, samesite="lax", max_age=43200, path=base_path or '/')
        return response

    def throttle(request):
        address = request.client.host if request.client else "unknown"
        attempts = [value for value in login_attempts.get(address, []) if value > now() - 300]
        if len(attempts) >= 15:
            raise HTTPException(429, "Too many sign-in attempts. Try again in five minutes.")
        if len(login_attempts) > 10000:
            login_attempts.clear()
        login_attempts[address] = attempts + [now()]

    @app.get("/readyz", tags=["System"])
    @app.get("/healthz", tags=["System"])
    def health():
        store.one("SELECT 1")
        if hasattr(store,'ensure_writable'):store.ensure_writable()
        if hasattr(app.state,'gateway'):app.state.gateway.cluster.ensure_serving()
        return {"status": "ok", "version": __version__}

    @app.get("/api/v1/session", tags=["Authentication"])
    def session(request: Request):
        setup = not store.one("SELECT id FROM users LIMIT 1")
        providers = store.all("SELECT id,name,protocol FROM identity_providers WHERE active=1")
        try:
            p = principal(request)
            if p["kind"] != "user":
                raise HTTPException(401)
            federation=store.one('SELECT protocol,config FROM identity_providers WHERE id=?',(p['user']['issuer'],))
            logout=f"/api/v1/identity-providers/{p['user']['issuer']}/logout" if federation and federation['protocol']=='saml' and json.loads(federation['config']).get('slo_url') else None
            return {"setup_required": setup, "user": safe_user(p["user"]), "csrf": p["csrf"], "identity_providers": providers, "version": __version__,'federated_logout':logout,**session_metadata()}
        except HTTPException:
            return {"setup_required": setup, "user": None, "identity_providers": providers, "version": __version__}

    @app.post("/api/v1/setup", tags=["Authentication"])
    def setup(body: Setup, request: Request):
        throttle(request)
        with store.transaction():
            if store.one("SELECT id FROM users LIMIT 1"):
                raise HTTPException(409, "Workspace has already been initialized")
            account = {"id": uid("usr_"), "email": body.email.strip().lower(), "name": body.name.strip(), "password_hash": hash_password(body.password), "role": "admin", "issuer": "local", "subject": body.email.strip().lower(), "active": 1, "created": now()}
            store.insert("users", account)
            store.audit(account["id"], "workspace.initialized", "workspace")
        return session_response(account)

    @app.post("/api/v1/login", tags=["Authentication"])
    def login(body: Login, request: Request):
        throttle(request)
        account = store.one("SELECT * FROM users WHERE issuer='local' AND subject=? AND active=1", (body.email.strip().lower(),))
        if not account or not verify_password(body.password, account["password_hash"]):
            raise HTTPException(401, "Email or password is incorrect")
        response=session_response(account)
        store.audit(account["id"], "session.login", account["id"])
        return response

    @app.post("/api/v1/logout", tags=["Authentication"])
    def logout(request: Request, p=Depends(user)):
        store.run("DELETE FROM sessions WHERE token_hash=?", (digest(request.cookies.get(cookie_name, "")),))
        response = JSONResponse({"ok": True})
        response.delete_cookie(cookie_name,path=base_path or '/')
        return response

    @app.get("/api/v1/connections", tags=["Connections"])
    def connections(p=Depends(user)):
        rows = store.all("SELECT id,name,endpoint,provider,status,last_checked,last_error,created,enabled,health_interval,manual_model,auth_header,auth_prefix,site,region,(secret IS NOT NULL AND secret!='') AS has_secret FROM connections ORDER BY created DESC")
        visible = set()
        if p['role'] != 'admin':
            for candidate in store.all("SELECT * FROM ports"):
                port = decoded_port(candidate)
                if service_allowed(store, p, port):
                    visible.update(port['deployments'])
        result = []
        for row in rows:
            deployments = [decoded_deployment(item) for item in store.all("SELECT * FROM deployments WHERE connection_id=?", (row["id"],)) if p['role']=='admin' or item['id'] in visible]
            if app.state.gateway.cluster.topology.multi_dc:
                observation=store.one('SELECT status,last_checked,last_error FROM connection_health WHERE site=? AND connection_id=?',(app.state.gateway.cluster.topology.site,row['id']))
                row={**row,**(observation or {'status':'configured','last_checked':None,'last_error':'Not checked from this site yet'})}
            if p['role']=='admin' or deployments:
                result.append({**row, 'endpoint':row['endpoint'] if p['role']=='admin' else '', 'has_secret':bool(row['has_secret']), 'models':deployments})
        return result

    @app.post("/api/v1/connections/discover", tags=["Connections"])
    async def probe(body: ConnectionInput, p=Depends(admin)):
        from .connectors import headers as auth_headers
        if body.provider=='anthropic' and 'auth_header' not in body.model_fields_set:
            body.auth_header='x-api-key';body.auth_prefix=''
        auth_headers("validation",body.auth_header,body.auth_prefix)
        if body.model.strip():
            return {"endpoint": endpoint_url(body.endpoint), "provider": body.provider if body.provider!='auto' else 'openai', "models": [{"model": body.model.strip(), "capabilities": ["chat", "streaming"], "evidence": "Manually configured; inference not yet tested"}]}
        app.state.gateway.secrets.client=app.state.client
        secret=await app.state.gateway.secrets.resolve(body.api_key)
        return await discover(app.state.client, body.endpoint, secret, body.provider, body.auth_header, body.auth_prefix)

    @app.post("/api/v1/connections", tags=["Connections"], status_code=201)
    async def create_connection(body: ConnectionInput, p=Depends(admin)):
        if body.site:
            target=store.one('SELECT region FROM sites WHERE id=?',(body.site,))
            if not target:raise HTTPException(422,'Register the model site before assigning this endpoint')
            if body.region and body.region!=target['region']:raise HTTPException(422,'Model region must match its registered site')
            body.region=target['region']
        elif body.region:raise HTTPException(422,'Assign a site when specifying a region')
        found = await probe(body, p)
        connection_id = uid("con_")
        with store.transaction():
            store.insert("connections", {"id": connection_id, "name": body.name.strip(), "endpoint": found["endpoint"], "provider": found["provider"], "secret": vault.encrypt(body.api_key), "status": "configured" if body.model else "connected", "last_checked": now() if not body.model else None, "manual_model": body.model, "auth_header": body.auth_header, "auth_prefix": body.auth_prefix, "created": now(),"site":body.site or "","region":body.region or ""})
            services = []
            for item in found["models"]:
                deployment_id = uid("dep_")
                store.insert("deployments", {"id": deployment_id, "connection_id": connection_id, "model": item["model"], "capabilities": encode(item["capabilities"]), "metadata_hash": item.get("metadata_hash", "")})
                name = re.sub(r"[^a-zA-Z0-9._/-]+", "-", item["model"]).strip("-./")[:80] or "model"
                if store.one("SELECT id FROM ports WHERE name=?", (name,)):
                    name += "-" + connection_id[-5:]
                port_id = uid("port_")
                store.insert("ports", {"id": port_id, "name": name, "description": "", "deployments": encode([deployment_id]), "created": now()})
                services.append({"id": port_id, "name": name})
            store.audit(p["id"], "connection.created", connection_id, {"name": body.name, "models": len(found["models"])})
        return {"id": connection_id, "provider": found["provider"], "models": found["models"], "services": services}

    @app.post("/api/v1/connections/{record_id}/check", tags=["Connections"])
    async def check_connection(record_id: str, p=Depends(admin)):
        row = store.one("SELECT * FROM connections WHERE id=?", (record_id,))
        if not row:
            raise HTTPException(404, "Connection not found")
        from .operations import check_connection_health
        return await check_connection_health(app,record_id,p['id'])

    @app.delete("/api/v1/connections/{record_id}", tags=["Connections"])
    def disconnect(record_id: str, p=Depends(admin)):
        if not store.one("SELECT id FROM connections WHERE id=?", (record_id,)):
            raise HTTPException(404, "Connection not found")
        with store.transaction():
            store.update("connections", record_id, {"status": "disconnected","enabled":0})
            store.audit(p["id"], "connection.disconnected", record_id)
        return {"ok": True}

    @app.patch("/api/v1/deployments/{record_id}", tags=["Connections"])
    def update_deployment(record_id: str, body: DeploymentInput, p=Depends(admin)):
        existing = store.one("SELECT * FROM deployments WHERE id=?", (record_id,))
        if not existing:
            raise HTTPException(404, "Deployment not found")
        verified = sorted(set(json.loads(existing["verified"])) & set(body.capabilities))
        store.update("deployments", record_id, {"capabilities": encode(body.capabilities), "verified": encode(verified), "input_price": body.input_price, "output_price": body.output_price, "enabled": int(body.enabled)})
        store.audit(p["id"], "deployment.updated", record_id, body.model_dump())
        return {"ok": True}

    @app.get("/api/v1/ports", tags=["AI services"])
    def ports(p=Depends(user)):
        result = []
        for row in store.all("SELECT * FROM ports ORDER BY created DESC"):
            port = decoded_port(row)
            if not service_allowed(store, p, port):
                continue
            candidates, decisions = app.state.gateway.candidates(port)
            result.append({**port, "ready": bool(candidates), "decisions": decisions, "fingerprint": app.state.gateway.fingerprint(port)})
        return result

    def port_fields(body):
        for record_id in body.deployments:
            if not store.one("SELECT id FROM deployments WHERE id=?", (record_id,)):
                raise HTTPException(422, "One of the selected deployments does not exist")
        if body.project_id and not store.one("SELECT id FROM projects WHERE id=?", (body.project_id,)):
            raise HTTPException(422, "Project not found")
        return {"placement":encode(body.placement.model_dump()),"log_content":int(body.log_content),"project_id":body.project_id,"verified_only":int(body.verified_only),"cache_ttl":body.cache_ttl,"max_input_chars":body.max_input_chars,"max_output_tokens":body.max_output_tokens,"name": body.name, "description": body.description, "deployments": encode(list(dict.fromkeys(body.deployments))), "requirements": encode(body.requirements), "strategy": body.strategy, "enabled": int(body.enabled)}

    @app.post("/api/v1/ports", tags=["AI services"], status_code=201)
    def create_port(body: PortInput, p=Depends(admin)):
        fields = port_fields(body)
        if store.one("SELECT id FROM ports WHERE name=?", (body.name,)):
            raise HTTPException(409, "An AI service with this name already exists")
        record_id = uid("port_")
        store.insert("ports", {"id": record_id, **fields, "created": now()})
        store.audit(p["id"], "service.created", record_id, body.model_dump())
        return {"id": record_id}

    @app.post("/api/v1/ports/{record_id}/preview", tags=["AI services"])
    def preview_port(record_id: str, body: PortInput, limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0), p=Depends(admin)):
        from .contracts import publication_impact
        with store.transaction():
            before = app.state.gateway.port(record_id)
            fields = port_fields(body)
            after = decoded_port({**before, **fields, "version": before["version"] + 1})
            candidates, decisions = app.state.gateway.candidates(after)
            impact = publication_impact(app.state.gateway, before, after, limit=limit, offset=offset)
            return {"current_version": before["version"], "proposed_version": after["version"], "ready": bool(candidates), "decisions": decisions, "removed_deployments": sorted(set(before["deployments"]) - set(after["deployments"])), "added_deployments": sorted(set(after["deployments"]) - set(before["deployments"])), "active_bindings_invalidated": impact['total'], "impact": impact, "current_fingerprint": app.state.gateway.fingerprint(before), "note": "Existing bindings will require a new handshake after publishing."}

    @app.patch("/api/v1/ports/{record_id}", tags=["AI services"])
    def update_port(record_id: str, body: PortInput, request: Request, p=Depends(admin)):
        existing = app.state.gateway.port(record_id)
        fields = port_fields(body)
        if store.one("SELECT id FROM ports WHERE name=? AND id!=?", (body.name, record_id)):
            raise HTTPException(409, "An AI service with this name already exists")
        from .operations import snapshot_port
        from .contracts import check_publication
        with store.transaction():
            existing = app.state.gateway.port(record_id)
            check_publication(request, app.state.gateway.fingerprint(existing))
            snapshot_port(store, existing, p['id'])
            store.update("ports", record_id, {**fields, "version": existing["version"] + 1})
            snapshot_port(store, app.state.gateway.port(record_id), p['id'])
        store.audit(p["id"], "service.updated", record_id, {"before": existing, "after": body.model_dump()})
        return {"id": record_id, "version": existing["version"] + 1}

    @app.get("/api/v1/ports/{record_id}/manifest", tags=["AI services"])
    def manifest(record_id: str, p=Depends(user)):
        port = app.state.gateway.port(record_id)
        app.state.gateway.authorize_port(p, port)
        return app.state.gateway.manifest(port)

    @app.get("/api/v1/projects", tags=["Projects and keys"])
    def projects(p=Depends(user)):
        allowed=project_ids(store,p)
        return [row for row in store.all("SELECT * FROM projects ORDER BY created") if row["id"] in allowed]

    @app.post("/api/v1/projects", tags=["Projects and keys"], status_code=201)
    def create_project(body: ProjectInput, p=Depends(admin)):
        record_id = uid("prj_")
        store.insert("projects", {"id": record_id, **body.model_dump(), "created": now()})
        store.audit(p["id"], "project.created", record_id, body.model_dump())
        return {"id": record_id}

    @app.patch("/api/v1/projects/{record_id}", tags=["Projects and keys"])
    def update_project(record_id: str, body: ProjectInput, p=Depends(admin)):
        if not store.one("SELECT id FROM projects WHERE id=?", (record_id,)):
            raise HTTPException(404, "Project not found")
        store.update("projects", record_id, body.model_dump())
        store.audit(p["id"], "project.updated", record_id, body.model_dump())
        return {"ok": True}

    @app.get("/api/v1/keys", tags=["Projects and keys"])
    def keys(p=Depends(admin)):
        return [{**row, "allowed_ports": json.loads(row["allowed_ports"])} for row in store.all("SELECT id,name,prefix,project_id,team_id,allowed_ports,rpm,active,expires,created FROM api_keys ORDER BY created DESC")]

    @app.post("/api/v1/keys", tags=["Projects and keys"], status_code=201)
    def create_key(body: KeyInput, p=Depends(admin)):
        if not store.one("SELECT id FROM projects WHERE id=?", (body.project_id,)):
            raise HTTPException(422, "Project not found")
        if body.team_id and not store.one("SELECT team_id FROM team_projects WHERE team_id=? AND project_id=?",(body.team_id,body.project_id)):
            raise HTTPException(422,"Team is not assigned to this project")
        for port_id in body.allowed_ports:
            app.state.gateway.port(port_id)
        if body.gateway_id:
            profile=app.state.gateway.policies.profile(body.gateway_id)
            if profile['config']['project_id'] and profile['config']['project_id']!=body.project_id:
                raise HTTPException(422,'Use the project assigned to this gateway')
            if profile['config']['team_ids'] and body.team_id not in profile['config']['team_ids']:
                raise HTTPException(422,'Use a team allowed by this gateway')
            if set(body.allowed_ports)-set(profile['config']['services']):
                raise HTTPException(422,'Key services must be published by this gateway')
        token, record_id = "plq_" + secrets.token_urlsafe(32), uid("key_")
        store.insert("api_keys", {"id": record_id, "name": body.name, "token_hash": digest(token), "prefix": token[:10], "project_id": body.project_id, "allowed_ports": encode(body.allowed_ports), "rpm": body.rpm, "expires": now() + body.expires_days * 86400 if body.expires_days else None, "team_id":body.team_id,"gateway_id":profile['id'] if body.gateway_id else None,"created_by":p["id"], "created": now()})
        store.audit(p["id"], "key.created", record_id, {"name": body.name, "project_id": body.project_id})
        return {"id": record_id, "token": token, "note": "This key is shown once. Store it securely."}

    @app.delete("/api/v1/keys/{record_id}", tags=["Projects and keys"])
    def revoke_key(record_id: str, p=Depends(admin)):
        if not store.one("SELECT id FROM api_keys WHERE id=?", (record_id,)):
            raise HTTPException(404, "Key not found")
        store.update("api_keys", record_id, {"active": 0})
        store.audit(p["id"], "key.revoked", record_id)
        return {"ok": True}

    @app.get("/api/v1/identity-providers", tags=["Identity"])
    def identities(p=Depends(admin)):
        result=store.all("SELECT id,name,issuer,client_id,default_role,active,created,protocol,config FROM identity_providers ORDER BY created")
        for row in result:
            config=json.loads(row['config'])
            config.pop('sp_private_key',None)
            row['config']=config
        return result

    @app.post("/api/v1/identity-providers", tags=["Identity"], status_code=201)
    async def create_identity(body: IdentityInput, p=Depends(admin)):
        issuer = endpoint_url(body.issuer)
        try:
            async with app.state.client.stream('GET',issuer + "/.well-known/openid-configuration",timeout=8) as result:
                result.raise_for_status()
                metadata=await response_json(result,100000)
            if metadata.get("issuer", "").rstrip("/") != issuer or not all(metadata.get(key) for key in ("authorization_endpoint", "token_endpoint", "jwks_uri")):
                raise ValueError("Invalid OIDC discovery document")
        except (httpx.HTTPError, ValueError):
            raise HTTPException(422, "Could not validate this issuer's OIDC discovery document")
        record_id = uid("idp_")
        store.insert("identity_providers", {"id": record_id, "name": body.name, "issuer": metadata["issuer"], "client_id": body.client_id, "secret": vault.encrypt(body.client_secret), "default_role": body.default_role, "created": now()})
        store.audit(p["id"], "identity.created", record_id, {"name": body.name, "issuer": issuer})
        return {"id": record_id}

    @app.delete("/api/v1/identity-providers/{record_id}", tags=["Identity"])
    def disable_identity(record_id: str, p=Depends(admin)):
        if not store.one("SELECT id FROM identity_providers WHERE id=?", (record_id,)):
            raise HTTPException(404, "Identity provider not found")
        store.update("identity_providers", record_id, {"active": 0})
        # Revoke sessions belonging to the disabled provider, without changing local accounts.
        store.run("DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE issuer=?)", (record_id,))
        store.audit(p["id"], "identity.disabled", record_id)
        return {"ok": True}

    oidc_secret_versions={}
    async def oidc_client(provider_id):
        row = store.one("SELECT * FROM identity_providers WHERE id=? AND active=1 AND protocol='oidc'", (provider_id,))
        if not row:
            raise HTTPException(404, "Identity provider is not available")
        name = "oidc_" + provider_id
        app.state.gateway.secrets.client=app.state.client
        secret=await app.state.gateway.secrets.resolve(vault.decrypt(row['secret']))
        client = oauth.create_client(name)
        if client is not None and oidc_secret_versions.get(name)!=digest(secret):
            oauth._clients.pop(name,None);oauth._registry.pop(name,None);client=None
        if client is None:
            oauth.register(name, client_id=row["client_id"], client_secret=secret, server_metadata_url=row["issuer"].rstrip("/") + "/.well-known/openid-configuration", client_kwargs={"scope": "openid email profile", "code_challenge_method": "S256",'verify':http_verify(),'trust_env':False})
            oidc_secret_versions[name]=digest(secret)
            client = oauth.create_client(name)
        return row, client

    @app.get("/auth/oidc/{provider_id}/login", tags=["Identity"])
    async def oidc_login(provider_id: str, request: Request):
        _, client = await oidc_client(provider_id)
        callback = (public_url or str(request.base_url).rstrip("/")) + f"/auth/oidc/{provider_id}/callback"
        return await client.authorize_redirect(request, callback)

    @app.get("/auth/oidc/{provider_id}/callback", tags=["Identity"])
    async def oidc_callback(provider_id: str, request: Request):
        row, client = await oidc_client(provider_id)
        try:
            token = await client.authorize_access_token(request, claims_options={"iss": {"essential": True, "value": row["issuer"]}})
            info = token.get("userinfo")
            if not token.get("id_token") or not info or not info.get("sub"):
                raise ValueError("No validated identity")
        except Exception:
            store.audit("anonymous", "identity.login_failed", provider_id)
            return RedirectResponse(base_path+"/?auth_error=oidc", 303)
        from .identity import federated_account
        account = federated_account(store,row,info)
        return session_response(account, request)

    @app.get("/api/v1/users", tags=["Identity"])
    def users(p=Depends(admin)):
        return [safe_user(row) for row in store.all("SELECT * FROM users ORDER BY created")]

    @app.post("/api/v1/users", tags=["Identity"], status_code=201)
    def create_user(body: UserInput, p=Depends(admin)):
        email = body.email.strip().lower()
        if store.one("SELECT id FROM users WHERE issuer='local' AND subject=?", (email,)):
            raise HTTPException(409, "This local account already exists")
        for project_id in body.project_ids:
            if not store.one("SELECT id FROM projects WHERE id=?",(project_id,)):
                raise HTTPException(422,"Project not found")
        record_id = uid("usr_")
        with store.transaction():
            store.insert("users", {"id": record_id, "email": email, "name": body.name, "password_hash": hash_password(body.password), "role": body.role, "subject": email, "created": now()})
            for project_id in set(body.project_ids):
                store.insert("memberships",{"project_id":project_id,"user_id":record_id,"role":"member","source":"manual"})
        store.audit(p["id"], "user.created", record_id, {"role": body.role})
        return {"id": record_id}

    @app.patch("/api/v1/users/{record_id}", tags=["Identity"])
    def update_user(record_id: str, body: UserUpdate, p=Depends(admin)):
        with store.transaction():
            row = store.one("SELECT * FROM users WHERE id=?", (record_id,))
            if not row:
                raise HTTPException(404, "User not found")
            if row["role"] == "admin" and row["issuer"] == "local" and (body.role != "admin" or not body.active) and store.one("SELECT COUNT(*) AS n FROM users WHERE role='admin' AND issuer='local' AND active=1")["n"] <= 1:
                raise HTTPException(409, "Keep at least one active local administrator")
            store.update("users", record_id, {"role": body.role, "active": int(body.active)})
            if not body.active:
                store.run("DELETE FROM sessions WHERE user_id=?", (record_id,))
            store.audit(p["id"], "user.updated", record_id, body.model_dump())
        return {"ok": True}

    @app.get("/api/v1/audit", tags=["System"])
    def audit(limit:int=200,offset:int=0,p=Depends(admin)):
        return [{**row, "detail": json.loads(row["detail"])} for row in store.all("SELECT * FROM audit ORDER BY created DESC,id DESC LIMIT ? OFFSET ?",(max(1,min(limit,500)),max(0,offset)))]

    @app.get("/api/v1/settings", tags=["System"])
    def settings(p=Depends(user)):
        return {"version": __version__, "storage": "PostgreSQL" if store.backend=="postgresql" else "SQLite / WAL", "deployment": "Shared database / multiple replicas" if store.backend=="postgresql" else "Single container / local storage", "protocol": PROTOCOL_VERSION, "content_logging": False, "secure_cookies": secure_cookies, "public_url": public_url or None, "identity_support": ["Local accounts", "OIDC with PKCE", "SAML 2.0", "SCIM 2.0 provisioning"], "planned_identity": [], "workspace_model": "Project memberships with scoped request visibility", "interfaces": ["Chat completions", "Embeddings", "Speech and transcription", "Responses", "Anthropic Messages", "Ollama native chat/generate"], "encryption": "Fernet authenticated encryption; volume-managed key or PLIQRA_ENCRYPTION_KEY"}

    @app.get("/.well-known/pliqra", tags=["Protocol"])
    def protocol(request: Request):
        from .protocol_extensions import EXTENSIONS
        return {**version_metadata(request.scope['pliqra.protocol']), "discovery": "/protocol/v1/services", "bind": "/protocol/v1/bindings", "authentication": "Bearer access key", "gateway_policy": "/protocol/v1/gateway", "features": ["capability-discovery", "contract-fingerprint", "explicit-binding", "decision-records",'extension-negotiation'], "invocation_interfaces": ["openai-chat-completions",'embeddings','audio-speech','audio-transcription'], 'extensions':EXTENSIONS,'conformance_profile':'pliqra-core/1'}

    @app.get("/protocol/v1/services", tags=["Protocol"])
    def services(request: Request, p=Depends(principal)):
        result = []
        for row in store.all("SELECT * FROM ports WHERE enabled=1"):
            port = decoded_port(row)
            try:
                app.state.gateway.authorize_port(p, port)
            except HTTPException:
                continue
            result.append({**app.state.gateway.manifest(port), 'protocol': request.scope['pliqra.protocol']})
        return {"protocol": request.scope['pliqra.protocol'], "services": result}

    @app.post("/protocol/v1/bindings", tags=["Protocol"], status_code=201)
    def bind(body: BindingInput, p=Depends(principal)):
        from .protocol_extensions import negotiate
        extensions=negotiate(body.required_extensions,body.optional_extensions)
        if set(body.requirements)&{'embeddings','audio_speech','audio_transcription'} and 'pliqra.operations/1' not in extensions:extensions.append('pliqra.operations/1')
        port = app.state.gateway.port(body.service)
        app.state.gateway.authorize_port(p, port)
        policy=app.state.gateway.policies.resolve(p,port,body.gateway_id)
        if policy and (('tools' in body.requirements and not policy['config']['allow_tools']) or ('vision' in body.requirements and not policy['config']['allow_vision'])):
            raise HTTPException(422,'Required capabilities are disabled by this gateway policy')
        eligible, decisions = app.state.gateway.candidates(port, body.requirements)
        if not eligible:
            raise HTTPException(422, {"message": "No deployment satisfies this contract", "decisions": decisions})
        if policy and policy['config']['require_verified']:
            eligible,decisions=app.state.gateway.candidates({**port,'verified_only':True},body.requirements)
            if not eligible:raise HTTPException(422,'No deployment has the gateway-required verified capabilities')
        binding = {"id": uid("bind_"), "principal": p["id"], "port_id": port["id"], "version": port["version"], "fingerprint": app.state.gateway.fingerprint(port), "requirements": encode(body.requirements), "expires": now() + body.ttl_seconds, "created": now(),"gateway_id":policy['id'] if policy else None,"gateway_fingerprint":policy['fingerprint'] if policy else None}
        binding['extensions']=encode(extensions)
        store.insert("bindings", binding)
        store.audit(p["id"], "binding.created", binding["id"], {"service": port["name"], "version": port["version"]})
        return {**binding, "requirements": body.requirements, 'extensions':extensions,"invoke": f"/protocol/v1/bindings/{binding['id']}/invoke", "service": port["name"],'placement':port.get('placement') or {},'operation_invocations':{name:f"/protocol/v1/bindings/{binding['id']}/operations/{name}" for name in ('embeddings','speech','transcription')} if 'pliqra.operations/1' in extensions else {}}

    @app.post("/protocol/v1/bindings/{record_id}/invoke", tags=["Protocol"])
    async def invoke(record_id: str, body: ChatInput, p=Depends(principal)):
        binding = store.one("SELECT * FROM bindings WHERE id=?", (record_id,))
        if not binding:
            raise HTTPException(404, "Binding not found")
        return await app.state.gateway.chat(body.model_dump(exclude_none=True), p, binding)

    @app.get("/v1/models", tags=["Inference"])
    def models(p=Depends(principal)):
        data = []
        for row in store.all("SELECT * FROM ports WHERE enabled=1"):
            port = decoded_port(row)
            try:
                app.state.gateway.authorize_port(p, port)
            except HTTPException:
                continue
            data.append({"id": port["name"], "object": "model", "created": int(port["created"]), "owned_by": "pliqra"})
        return {"object": "list", "data": data}

    @app.post("/v1/chat/completions", tags=["Inference"])
    async def chat(body: ChatInput, request: Request, p=Depends(principal)):
        from .contracts import header_binding, BINDING_HEADER
        binding = header_binding(store, request)
        response = await app.state.gateway.chat(body.model_dump(exclude_none=True), p, binding)
        if binding:
            response.headers[BINDING_HEADER] = binding['id']
        return response

    app.state.context = {"store":store,"vault":vault,"principal":principal,"user":user,"admin":admin,"session_response":session_response,"public_url":public_url,"secure_cookies":secure_cookies,"oauth":oauth,"PortInput":PortInput,"port_fields":port_fields,"create_key":create_key,"KeyInput":KeyInput,"bind":bind,"BindingInput":BindingInput,"ChatInput":ChatInput,'base_path':base_path,'cookie_name':cookie_name,'organization':organization}
    from .operations import mount as mount_operations
    from .identity import mount as mount_identity
    from .reporting import mount as mount_reporting
    from .evaluation import mount as mount_evaluation
    mount_operations(app)
    mount_identity(app)
    mount_reporting(app)
    mount_evaluation(app)
    from .experience import mount as mount_experience
    mount_experience(app)
    from .fabric import mount as mount_fabric
    mount_fabric(app)
    from .model_operations import mount as mount_model_operations
    mount_model_operations(app)
    from .secrets import mount as mount_secrets
    from .workload_identity import mount as mount_workload_identity
    mount_secrets(app)
    mount_workload_identity(app)
    from .prompts import mount as mount_prompts
    mount_prompts(app)
    from .batches import mount as mount_batches
    mount_batches(app)
    from .quality import mount as mount_quality
    mount_quality(app)
    from .native import mount as mount_native
    mount_native(app)
    from .platform import mount as mount_platform
    mount_platform(app)
    from .topology import mount as mount_topology
    mount_topology(app)
    from .fleet import mount as mount_fleet
    mount_fleet(app)
    from .identity_profiles import mount as mount_identity_profiles
    mount_identity_profiles(app)
    if organization is None:
        from .organizations import mount as mount_organizations
        mount_organizations(app)

    @app.get('/sdk/pliqra_client.py',tags=['Protocol'])
    def download_client():
        return FileResponse(STATIC.parent.parent/'sdk'/'pliqra_client.py',filename='pliqra_client.py',media_type='text/x-python')

    @app.get('/sdk/{language}/{filename}',tags=['Protocol'])
    def download_sdk(language:str,filename:str):
        allowed={'javascript':{'pliqra.mjs','pliqra.d.mts'},'go':{'pliqra.go','go.mod'}}
        if filename not in allowed.get(language,set()):raise HTTPException(404,'SDK file not found')
        return FileResponse(STATIC.parent.parent/'sdk'/language/filename,filename=filename,media_type='text/plain')

    @app.get('/sdk/conformance.py',tags=['Protocol'])
    def download_conformance():
        return FileResponse(STATIC.parent.parent/'sdk'/'conformance.py',filename='conformance.py',media_type='text/x-python')

    @app.get('/protocol/specification',tags=['Protocol'])
    def protocol_specification():
        return FileResponse(STATIC.parent.parent/'docs'/'PROTOCOL.md',filename='pliqra-protocol.md',media_type='text/markdown')

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        html=(STATIC/'index.html').read_text()
        html=html.replace('href="/static/',f'href="{base_path}/static/').replace('src="/static/',f'src="{base_path}/static/')
        return HTMLResponse(html.replace('<meta charset="UTF-8">','<meta charset="UTF-8"><meta name="pliqra-base" content="'+base_path+'">'))

    app.add_middleware(ProtocolVersionMiddleware)
    from .contracts import BindingHeaderMiddleware
    app.add_middleware(BindingHeaderMiddleware)
    return app
