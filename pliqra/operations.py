from .config import env
import asyncio
import base64
import hashlib
import json
import os
import secrets
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Annotated, Literal

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator

from .access import project_ids, require_project, service_allowed
from .connectors import discover, headers
from .db import encode, now, uid, SCHEMA_VERSION
from .gateway import decoded_port
from .security import bearer_token, digest, endpoint_url, hash_password, verify_password, validate_encryption


from .topology import SiteID,observe_connection,observe_deployment


class ConnectionPatch(BaseModel):
    site:SiteID|Literal['']|None=None
    region:SiteID|Literal['']|None=None
    name: Annotated[str, Field(min_length=1, max_length=100)]
    endpoint: Annotated[str, Field(max_length=2000)]
    provider: Literal["openai", "ollama", "responses", "anthropic"]
    api_key: Annotated[str | None, Field(max_length=4096)] = None
    enabled: bool = True
    health_interval: Annotated[int, Field(ge=0, le=86400)] = 300
    manual_model: Annotated[str, Field(max_length=256)] = ""
    auth_header: Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9-]{0,63}$")] = "Authorization"
    auth_prefix: Annotated[str, Field(max_length=32, pattern=r"^[A-Za-z0-9 -]*$")] = "Bearer"


class Member(BaseModel):
    user_id: str
    role: Literal["member", "manager"] = "member"


class TeamInput(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=100)]
    members: Annotated[list[str], Field(max_length=1000)] = []
    projects: Annotated[list[dict], Field(max_length=1000)] = []


class Reconciliation(BaseModel):
    cost_micros: Annotated[int, Field(ge=0, le=10**15)]
    input_tokens: Annotated[int | None, Field(ge=0)] = None
    output_tokens: Annotated[int | None, Field(ge=0)] = None
    note: Annotated[str, Field(min_length=3, max_length=1000)]


class PasswordChange(BaseModel):
    current_password: str = ""
    new_password: Annotated[str, Field(min_length=12, max_length=512)]


class BackupInput(BaseModel):
    password: Annotated[str, Field(min_length=12, max_length=512)]


class RestoreInput(BackupInput):
    archive: Annotated[str, Field(max_length=90_000_000)]
    confirmation: Literal["RESTORE"]


class RuntimeInput(BaseModel):
    retention_days: Annotated[int, Field(ge=32, le=3650)] = 90
    max_concurrency: Annotated[int, Field(ge=1, le=200)] = 64
    max_batch_concurrency: Annotated[int, Field(ge=1, le=32)] = 2
    health_checks: bool = True


class ImportInput(BaseModel):
    document: dict
    mapping: dict[str, str]
    name: Annotated[str | None, Field(max_length=100)] = None
    project_id: str | None = None


def snapshot_port(store, port, actor):
    store.run("INSERT INTO port_versions VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING", (port["id"], port["version"], encode(port), actor, now()))


def backup_bytes(store, vault, password):
    if store.backend=='postgresql':
        from .logical_archive import backup
        return backup(store,vault,password)
    salt = secrets.token_bytes(16)
    key = base64.urlsafe_b64encode(hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600000))
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "snapshot.sqlite3"
        destination = sqlite3.connect(path)
        with store.lock:
            store.conn.backup(destination)
        destination.close()
        if path.stat().st_size>32_000_000:raise HTTPException(413,'Workspace exceeds the 32 MB online backup limit; use a native database/volume snapshot')
        payload = encode({"format": "pliqra-backup/1", "schema": SCHEMA_VERSION, "created": now(), "database": base64.b64encode(path.read_bytes()).decode(), "key": vault.key.decode()}).encode()
    return b"PLQBACK01" + salt + Fernet(key).encrypt(payload)


def restore_bytes(store, vault, archive, password):
    if archive[:9]==b'PLQLOGI01':
        from .logical_archive import restore
        return restore(store,vault,archive,password)
    if store.backend=='postgresql':raise HTTPException(422,'Use a PostgreSQL logical backup for this workspace')
    if archive[:9] not in (b"PLQBACK01",b"MPBACKUP1") or len(archive) < 100:
        raise HTTPException(422, "This is not a Pliqra encrypted backup")
    salt = archive[9:25]
    key = base64.urlsafe_b64encode(hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600000))
    try:
        payload = json.loads(Fernet(key).decrypt(archive[25:]))
        if payload["format"] not in ("pliqra-backup/1","modelport-backup/1") or payload["schema"] not in (2,3,4,5,6,SCHEMA_VERSION):
            raise ValueError()
        replacement = payload["key"].encode()
        Fernet(replacement)
        raw = base64.b64decode(payload["database"], validate=True)
    except (InvalidToken, ValueError, KeyError, TypeError):
        raise HTTPException(422, "Backup password is incorrect or the archive is damaged")
    environment_key = vault.external
    if environment_key and vault.key != replacement:
        raise HTTPException(409, "The backup uses a different encryption key. Restore using the matching PLIQRA_ENCRYPTION_KEY.")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "restore.sqlite3"
        path.write_bytes(raw)
        try:
            source = sqlite3.connect(path)
            if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or source.execute("PRAGMA user_version").fetchone()[0] not in (2,3,4,5,6,SCHEMA_VERSION):
                raise ValueError()
            if not source.execute("SELECT id FROM users WHERE role='admin' AND issuer='local' AND active=1").fetchone():
                raise ValueError()
            if source.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError()
            validate_encryption(source,replacement)
        except (sqlite3.Error, ValueError, TypeError, AttributeError, InvalidToken):
            if 'source' in locals():source.close()
            raise HTTPException(422, "Backup database failed validation or has no active local administrator")
        with store.lock:
            # Keep a consistent local recovery snapshot until the operation has completed.
            recovery = sqlite3.connect(store.directory / "pre-restore.sqlite3")
            store.conn.backup(recovery)
            recovery.close()
            os.chmod(store.directory / 'pre-restore.sqlite3',0o600)
            old_key = vault.key
            recovery_key = store.directory / "pre-restore.key"
            recovery_key.write_bytes(old_key)
            os.chmod(recovery_key, 0o600)
            (store.directory/'pending-restore').write_text('rollback-on-restart')
            try:
                source.backup(store.conn)
                store.migrate()
                if not environment_key:
                    key_path = store.directory / "encryption.key"
                    temp_key = store.directory / "encryption.key.new"
                    temp_key.write_bytes(replacement)
                    os.chmod(temp_key, 0o600)
                    os.replace(temp_key, key_path)
                vault.replace_key(replacement)
                store.run("DELETE FROM sessions")
                store.run("DELETE FROM saml_flows")
                store.run("DELETE FROM cache_entries")
                store.run("UPDATE events SET updated=?,status='interrupted',finished=?,error='Restored from backup during an unfinished request' WHERE status='pending'", (store.clock(),now(),))
                store.run("UPDATE evaluations SET status='interrupted',finished=?,error='Restored from backup' WHERE status IN ('queued','running')", (now(),))
                store.run("UPDATE attempts SET outcome='interrupted',finished=? WHERE outcome='pending'",(now(),))
                store.run("UPDATE batch_items SET status='interrupted',error='Restored during execution; no automatic replay' WHERE status='running'")
                store.run("UPDATE batches SET status='queued',node_id=NULL WHERE status='running'")
                store.run("UPDATE behavior_runs SET status='interrupted',finished=?,error='Restored from backup; no automatic replay' WHERE status='running'", (store.clock(),))
                store.run("UPDATE behavior_schedules SET enabled=0,last_status='paused',last_error='Re-enable scheduled checks after reviewing the restored workspace'")
                store.run('DELETE FROM runtime_leases')
                store.run('DELETE FROM runtime_nodes')
                (store.directory/'pending-restore').unlink(missing_ok=True)
            except BaseException:
                restore = sqlite3.connect(store.directory / "pre-restore.sqlite3")
                restore.backup(store.conn)
                restore.close()
                vault.replace_key(old_key)
                if not environment_key:
                    (store.directory / "encryption.key").write_bytes(old_key)
                (store.directory/'pending-restore').unlink(missing_ok=True)
                raise
            finally:
                source.close()
    return {"ok": True, "sign_in_required": True}


async def check_connection_health(app, record_id, actor="system"):
    store, vault = app.state.store, app.state.vault
    generation=app.state.gateway.generation
    topology=app.state.gateway.cluster.topology
    row = store.one("SELECT * FROM connections WHERE id=?", (record_id,))
    if not row:
        raise HTTPException(404, "Connection not found")
    if not row["enabled"]:
        raise HTTPException(409, "Connection is disabled")
    def ensure_current():
        current=store.one('SELECT * FROM connections WHERE id=?',(record_id,))
        fields=('endpoint','provider','secret','manual_model','auth_header','auth_prefix','enabled','site','region')
        if generation!=app.state.gateway.generation or app.state.gateway.maintenance or not current or any(current[k]!=row[k] for k in fields):
            raise HTTPException(409,'Workspace or connection changed during the health check; retry')
    app.state.gateway.secrets.client=app.state.client
    secret=await app.state.gateway.secrets.resolve(vault.decrypt(row["secret"]))
    try:
        found = await discover(app.state.client, row["endpoint"], secret, row["provider"], row["auth_header"], row["auth_prefix"])
    except HTTPException:
        if row["manual_model"]:
            try:
                async with app.state.client.stream('GET',row["endpoint"], headers=headers(secret, row["auth_header"], row["auth_prefix"]), timeout=5) as response:
                    if response.status_code < 500 and response.status_code not in (401,403):
                        with store.transaction():
                            ensure_current()
                            if topology.multi_dc:
                                observe_connection(store,topology.site,record_id,'reachable','Model-list discovery unavailable')
                                for item in store.all('SELECT id FROM deployments WHERE connection_id=?',(record_id,)):observe_deployment(store,topology.site,item['id'],available=1)
                            store.update("connections", record_id, {"status":"reachable", "last_checked":now(), "last_error":"HTTP endpoint reachable; model-list discovery unavailable"})
                        return {"provider":row["provider"],"models":[],"status":"reachable","inference_verified":False}
            except httpx.HTTPError:
                pass
        with store.transaction():
            ensure_current()
            if topology.multi_dc:
                observe_connection(store,topology.site,record_id,'unreachable','Endpoint or credentials failed the health check')
                for item in store.all('SELECT id FROM deployments WHERE connection_id=?',(record_id,)):observe_deployment(store,topology.site,item['id'],available=0)
            store.update("connections", record_id, {"status":"unreachable", "last_checked":now(), "last_error":"Endpoint or credentials failed the health check"})
        raise HTTPException(502, "Endpoint or credentials failed the health check")
    if generation!=app.state.gateway.generation or app.state.gateway.maintenance:
        raise HTTPException(409,'Workspace changed during the health check; retry')
    changed = []
    with store.transaction():
        ensure_current()
        seen = set()
        for item in found["models"]:
            seen.add(item["model"])
            existing = store.one("SELECT * FROM deployments WHERE connection_id=? AND model=?", (record_id,item["model"]))
            if not existing:
                store.insert("deployments", {"id":uid("dep_"),"connection_id":record_id,"model":item["model"],"capabilities":encode(item["capabilities"]),"metadata_hash":item.get("metadata_hash","")})
                changed.append("New model: " + item["model"])
            else:
                fields = {"metadata_hash":item.get("metadata_hash","")}
                if topology.multi_dc:observe_deployment(store,topology.site,existing['id'],available=1)
                else:fields['available']=1
                if existing["metadata_hash"] and fields["metadata_hash"] and existing["metadata_hash"] != fields["metadata_hash"]:
                    fields.update(verified="[]", verified_at=None)
                    changed.append("Model metadata changed; verification reset: " + item["model"])
                store.update("deployments",existing["id"],fields)
        for item in store.all("SELECT * FROM deployments WHERE connection_id=?",(record_id,)):
            if item["model"] not in seen and not row["manual_model"]:
                if topology.multi_dc:observe_deployment(store,topology.site,item['id'],available=0)
                else:store.update("deployments",item["id"],{"available":0})
                if item["available"]:
                    changed.append("Model no longer advertised: " + item["model"])
        if topology.multi_dc:observe_connection(store,topology.site,record_id,'connected')
        store.update("connections",record_id,{"status":"connected","last_checked":now(),"last_error":None})
        for message in changed:
            store.insert("notifications",{"id":uid("note_"),"kind":"model-drift","target":record_id,"message":message,"created":now()})
        store.audit(actor,"connection.checked",record_id,{"changes":changed})
    return {**found,"changes":changed}


async def maintain(app):
    store = app.state.store
    tick = 0
    try:
        while True:
            await asyncio.sleep(10)
            tick += 1
            if app.state.gateway.maintenance:
                continue
            if store.setting("runtime",{}).get("health_checks",True):
                if app.state.gateway.cluster.topology.multi_dc:
                    due=store.all("SELECT c.id FROM connections c LEFT JOIN connection_health h ON h.connection_id=c.id AND h.site=? WHERE c.enabled=1 AND c.health_interval>0 AND (h.last_checked IS NULL OR h.last_checked+c.health_interval<?) LIMIT 5",(app.state.gateway.cluster.topology.site,store.clock()))
                else:due = store.all("SELECT id FROM connections WHERE enabled=1 AND health_interval>0 AND (last_checked IS NULL OR last_checked+health_interval<?) LIMIT 5",(now(),))
                for row in due:
                    try:
                        await check_connection_health(app,row["id"])
                    except HTTPException:
                        pass
            if tick % 6 == 0:
                days = store.setting("runtime",{}).get("retention_days",90)
                cutoff = now()-days*86400
                with store.transaction():
                    store.run("DELETE FROM event_content WHERE id IN (SELECT id FROM events WHERE started<? AND status!='pending' AND reserved_micros=0)",(cutoff,))
                    store.run("DELETE FROM attempts WHERE event_id IN (SELECT id FROM events WHERE started<? AND status!='pending' AND reserved_micros=0)",(cutoff,))
                    store.run("DELETE FROM events WHERE started<? AND status!='pending' AND reserved_micros=0",(cutoff,))
                    store.run("DELETE FROM behavior_runs WHERE created<? AND status!='running'", (cutoff,))
                    store.run("DELETE FROM probes WHERE created<?", (cutoff,))
                    store.run("DELETE FROM cache_entries WHERE expires<?",(now(),))
                    store.run("DELETE FROM gateway_requests WHERE started<? AND status!='pending'",(now()-2592000,))
                    store.run("DELETE FROM guardrail_decisions WHERE created<?",(cutoff,))
                    store.run("DELETE FROM sessions WHERE expires<?",(now(),))
                    store.run("DELETE FROM bindings WHERE expires<?",(now(),))
                    store.run("DELETE FROM saml_flows WHERE expires<?",(now(),))
                    store.run("DELETE FROM saml_assertions WHERE expires<?",(now(),))
                    store.run("DELETE FROM workload_assertions WHERE expires<?",(now(),))
                    store.run("DELETE FROM auth_attempts WHERE created<?",(store.clock()-300,))
                    store.run("DELETE FROM api_keys WHERE workload_provider_id IS NOT NULL AND expires<?",(cutoff,))
    except asyncio.CancelledError:
        raise


def mount(app):
    ctx = app.state.context
    store,vault,admin,user = ctx["store"],ctx["vault"],ctx["admin"],ctx["user"]

    @app.patch("/api/v1/connections/{record_id}",tags=["Connections"])
    def edit_connection(record_id: str,body:ConnectionPatch,p=Depends(admin)):
        existing=store.one("SELECT * FROM connections WHERE id=?",(record_id,))
        if not existing:raise HTTPException(404,"Connection not found")
        if 'site' not in body.model_fields_set:
            body.site=existing.get('site') or None
            if 'region' not in body.model_fields_set:body.region=existing.get('region') or None
        if body.site:
            target=store.one('SELECT region FROM sites WHERE id=?',(body.site,))
            if not target:raise HTTPException(422,'Register the model site before assigning this endpoint')
            if body.region and body.region!=target['region']:raise HTTPException(422,'Model region must match its registered site')
            body.region=target['region']
        elif body.region:raise HTTPException(422,'Assign a site when specifying a region')
        endpoint=endpoint_url(body.endpoint)
        headers("validation",body.auth_header,body.auth_prefix)
        fields=body.model_dump(exclude={"api_key"})
        fields.update(site=body.site or '',region=body.region or '',endpoint=endpoint,status="configured" if body.enabled else "disconnected",last_checked=None,last_error=None)
        if body.api_key is not None:fields["secret"]=vault.encrypt(body.api_key)
        with store.transaction():
            store.update("connections",record_id,fields)
            store.run('DELETE FROM cache_entries')
            store.run('DELETE FROM connection_health WHERE connection_id=?',(record_id,))
            store.run('DELETE FROM deployment_health WHERE deployment_id IN (SELECT id FROM deployments WHERE connection_id=?)',(record_id,))
            if endpoint!=existing["endpoint"] or body.provider!=existing["provider"]:
                store.run("UPDATE deployments SET verified='[]',verified_at=NULL,metadata_hash='' WHERE connection_id=?",(record_id,))
            store.audit(p["id"],"connection.updated",record_id,{"fields":list(fields),"credentials_rotated":body.api_key is not None})
        return {"ok":True}

    @app.get("/api/v1/memberships",tags=["Access"])
    def memberships(p=Depends(user)):
        rows=store.all("SELECT m.*,u.name,u.email,p.name AS project_name FROM memberships m JOIN users u ON u.id=m.user_id JOIN projects p ON p.id=m.project_id")
        managed=set(project_ids(store,p,True))
        return [r for r in rows if p["role"]=="admin" or r["user_id"]==p["id"] or r["project_id"] in managed]

    @app.post("/api/v1/projects/{project_id}/members",tags=["Access"])
    def add_member(project_id:str,body:Member,p=Depends(admin)):
        require_project(store,p,project_id,True)
        if not store.one("SELECT id FROM users WHERE id=?",(body.user_id,)):raise HTTPException(404,"User not found")
        store.run("INSERT INTO memberships VALUES (?,?,?,?) ON CONFLICT(project_id,user_id,source) DO UPDATE SET role=excluded.role",(project_id,body.user_id,body.role,"manual"))
        store.audit(p["id"],"membership.saved",project_id,{"user_id":body.user_id,"role":body.role})
        return {"ok":True}

    @app.delete("/api/v1/projects/{project_id}/members/{user_id}",tags=["Access"])
    def remove_member(project_id:str,user_id:str,p=Depends(admin)):
        require_project(store,p,project_id,True)
        store.run("DELETE FROM memberships WHERE project_id=? AND user_id=? AND source='manual'",(project_id,user_id))
        store.audit(p["id"],"membership.removed",project_id,{"user_id":user_id})
        return {"ok":True}

    @app.get("/api/v1/teams",tags=["Access"])
    def teams(p=Depends(user)):
        rows=store.all("SELECT * FROM teams ORDER BY name")
        return [{**r,"members":[x["user_id"] for x in store.all("SELECT user_id FROM team_members WHERE team_id=?",(r["id"],))],"projects":store.all("SELECT project_id,role FROM team_projects WHERE team_id=?",(r["id"],))} for r in rows if p["role"]=="admin" or store.one("SELECT team_id FROM team_members WHERE team_id=? AND user_id=?",(r["id"],p["id"]))]

    def save_team(record_id,body,p,new=False):
        for member in body.members:
            if not store.one("SELECT id FROM users WHERE id=?",(member,)):raise HTTPException(422,"Unknown team member")
        for project in body.projects:
            if project.get("role","member") not in ("member","manager") or not store.one("SELECT id FROM projects WHERE id=?",(project.get("project_id"),)):raise HTTPException(422,"Invalid project grant")
        with store.transaction():
            if new:store.insert("teams",{"id":record_id,"name":body.name.strip(),"created":now()})
            else:store.update("teams",record_id,{"name":body.name.strip()})
            store.run("DELETE FROM team_members WHERE team_id=?",(record_id,))
            store.run("DELETE FROM team_projects WHERE team_id=?",(record_id,))
            for member in set(body.members):store.insert("team_members",{"team_id":record_id,"user_id":member})
            for project in {r["project_id"]:r for r in body.projects}.values():store.insert("team_projects",{"team_id":record_id,"project_id":project["project_id"],"role":project.get("role","member")})
            store.audit(p["id"],"team.saved",record_id,body.model_dump())
        return {"id":record_id}

    @app.post("/api/v1/teams",tags=["Access"],status_code=201)
    def create_team(body:TeamInput,p=Depends(admin)):return save_team(uid("team_"),body,p,True)

    @app.patch("/api/v1/teams/{record_id}",tags=["Access"])
    def edit_team(record_id:str,body:TeamInput,p=Depends(admin)):
        if not store.one("SELECT id FROM teams WHERE id=?",(record_id,)):raise HTTPException(404,"Team not found")
        return save_team(record_id,body,p)

    @app.post("/api/v1/keys/{record_id}/rotate",tags=["Projects and keys"])
    def rotate_key(record_id:str,p=Depends(admin)):
        old=store.one("SELECT * FROM api_keys WHERE id=? AND active=1",(record_id,))
        if not old:raise HTTPException(404,"Active key not found")
        token="plq_"+secrets.token_urlsafe(32)
        new={**old,"id":uid("key_"),"token_hash":digest(token),"prefix":token[:10],"created":now(),"created_by":old.get("created_by") or p["id"]}
        with store.transaction():
            store.update("api_keys",record_id,{"active":0})
            store.insert("api_keys",new)
            store.audit(p["id"],"key.rotated",record_id,{"replacement":new["id"]})
        return {"id":new["id"],"token":token}

    @app.get("/api/v1/ports/{record_id}/versions",tags=["AI services"])
    def versions(record_id:str,p=Depends(admin)):
        port=app.state.gateway.port(record_id)
        snapshot_port(store,port,p["id"])
        return [{**row,"config":json.loads(row["config"])} for row in store.all("SELECT * FROM port_versions WHERE port_id=? ORDER BY version DESC",(record_id,))]

    @app.post("/api/v1/ports/{record_id}/rollback/{version}",tags=["AI services"])
    def rollback(record_id:str,version:int,p=Depends(admin)):
        current=app.state.gateway.port(record_id)
        row=store.one("SELECT config FROM port_versions WHERE port_id=? AND version=?",(record_id,version))
        if not row:raise HTTPException(404,"Service version not found")
        value=ctx["PortInput"](**json.loads(row["config"]))
        fields=ctx["port_fields"](value)
        with store.transaction():
            snapshot_port(store,current,p["id"])
            store.update("ports",record_id,{**fields,"version":current["version"]+1})
            snapshot_port(store,app.state.gateway.port(record_id),p["id"])
            store.audit(p["id"],"service.rollback",record_id,{"source_version":version,"new_version":current["version"]+1})
        return {"ok":True,"version":current["version"]+1}

    @app.get("/api/v1/ports/{record_id}/export",tags=["Protocol"])
    def export_service(record_id:str,p=Depends(user)):
        port=app.state.gateway.port(record_id)
        app.state.gateway.authorize_port(p,port)
        fields={k:port[k] for k in ctx["PortInput"].model_fields if k not in ("deployments","project_id")}
        refs=[]
        for index,dep_id in enumerate(port["deployments"]):
            row=store.one("SELECT d.model,c.name,c.provider FROM deployments d JOIN connections c ON c.id=d.connection_id WHERE d.id=?",(dep_id,))
            if row:refs.append({"ref":f"deployment-{index}","model":row["model"],"connection_label":row["name"],"adapter":row["provider"]})
        return {"schema":"pliqra/service-export/v1","service":fields,"deployment_refs":refs,"credentials_included":False}

    @app.post("/api/v1/ports/import",tags=["Protocol"],status_code=201)
    def import_service(body:ImportInput,p=Depends(admin)):
        document=body.document
        if document.get("schema") not in ("pliqra/service-export/v1","model-port/service-export/v1"):raise HTTPException(422,"Unsupported service export format")
        if not isinstance(document.get('service'),dict):raise HTTPException(422,'Service configuration must be an object')
        if set(document.get("service",{}))-set(ctx["PortInput"].model_fields):raise HTTPException(422,"Export contains unsupported service policies")
        refs=document.get("deployment_refs",[])
        if not isinstance(refs,list) or not refs or len(refs)>100 or any(not isinstance(r,dict) or not isinstance(r.get('ref'),str) for r in refs):raise HTTPException(422,"Export must contain deployment references")
        mapped=[]
        for ref in refs:
            target=body.mapping.get(ref.get("ref"))
            if not target or not store.one("SELECT id FROM deployments WHERE id=?",(target,)):raise HTTPException(422,"Map every deployment reference to a local model")
            mapped.append(target)
        value=ctx["PortInput"](**{**document["service"],"name":body.name or document["service"].get("name"),"deployments":mapped,"project_id":body.project_id})
        fields=ctx["port_fields"](value)
        if store.one("SELECT id FROM ports WHERE name=?",(value.name,)):raise HTTPException(409,"Choose an unused service name")
        record_id=uid("port_")
        store.insert("ports",{"id":record_id,**fields,"created":now()})
        store.audit(p["id"],"service.imported",record_id)
        return {"id":record_id,"name":value.name}

    @app.get("/api/v1/operations",tags=["Operations"])
    def operations(p=Depends(admin)):
        return {"runtime":store.setting("runtime",{"retention_days":90,"max_concurrency":64,"health_checks":True}),"active_requests":app.state.gateway.active,"database_bytes":store.database_size(),"cache_entries":store.one("SELECT COUNT(*) AS n FROM cache_entries WHERE expires>?",(now(),))["n"],"unresolved":store.all("SELECT id,started,model,port_name,status,reserved_micros,cost_micros FROM events WHERE status!='pending' AND (reserved_micros>0 OR (cost_micros IS NULL AND status IN ('incomplete','interrupted'))) ORDER BY started DESC LIMIT 200"),"notifications":store.all("SELECT * FROM notifications WHERE resolved=0 ORDER BY created DESC LIMIT 100"),"capability_probes":store.all("SELECT p.*,d.model FROM probes p LEFT JOIN deployments d ON d.id=p.deployment_id ORDER BY p.created DESC LIMIT 100")}

    @app.patch("/api/v1/operations",tags=["Operations"])
    def runtime(body:RuntimeInput,p=Depends(admin)):
        store.set_setting("runtime",body.model_dump())
        store.audit(p["id"],"runtime.updated","workspace",body.model_dump())
        return {"ok":True}

    @app.post("/api/v1/events/{record_id}/reconcile",tags=["Usage"])
    def reconcile(record_id:str,body:Reconciliation,p=Depends(admin)):
        row=store.one("SELECT * FROM events WHERE id=?",(record_id,))
        if not row:raise HTTPException(404,"Request not found")
        if row["status"]=="pending":raise HTTPException(409,"Wait for the request to finish")
        fields={"cost_micros":body.cost_micros,"reserved_micros":0,"reconciled_by":p["id"],"reconciliation_note":body.note}
        if body.input_tokens is not None and body.output_tokens is not None:fields.update(input_tokens=body.input_tokens,output_tokens=body.output_tokens,usage_source="reconciled")
        with store.transaction():
            store.update("events",record_id,fields)
            if body.input_tokens is not None and body.output_tokens is not None and store.one("SELECT id FROM gateway_requests WHERE id=?",(record_id,)):
                store.update("gateway_requests",record_id,{"tokens":body.input_tokens+body.output_tokens})
            store.audit(p["id"],"usage.reconciled",record_id,{"before_cost":row["cost_micros"],"after":fields})
        return {"ok":True}

    @app.post("/api/v1/cache/clear",tags=["Operations"])
    def clear_cache(p=Depends(admin)):
        store.run("DELETE FROM cache_entries")
        store.audit(p["id"],"cache.cleared","workspace")
        return {"ok":True}

    @app.post("/api/v1/notifications/{record_id}/resolve",tags=["Operations"])
    def resolve(record_id:str,p=Depends(admin)):
        store.update("notifications",record_id,{"resolved":1})
        return {"ok":True}

    @app.post("/api/v1/backups",tags=["Operations"])
    def backup(body:BackupInput,p=Depends(admin)):
        archive=backup_bytes(store,vault,body.password)
        store.audit(p["id"],"backup.created","workspace")
        return Response(archive,media_type="application/octet-stream",headers={"Content-Disposition":'attachment; filename="pliqra-backup.mpb"'})

    @app.post("/api/v1/backups/restore",tags=["Operations"])
    def restore(body:RestoreInput,p=Depends(admin)):
        try:archive=base64.b64decode(body.archive,validate=True)
        except ValueError:raise HTTPException(422,"Invalid backup encoding")
        with app.state.gateway.state_lock:
            with store.transaction():
                if store.one('SELECT id FROM runtime_leases LIMIT 1') or app.state.gateway.maintenance or store.one("SELECT id FROM evaluations WHERE status IN ('queued','running') LIMIT 1") or store.one("SELECT id FROM batches WHERE status IN ('queued','running') LIMIT 1") or store.one("SELECT id FROM behavior_runs WHERE status='running' LIMIT 1"):raise HTTPException(409,"Stop active requests, provider checks, evaluations and batches before restoring")
                app.state.gateway.maintenance=True
        try:
            result=restore_bytes(store,vault,archive,body.password)
            with store.transaction():
                store.run('INSERT INTO runtime_nodes (id,heartbeat,site,region) VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET heartbeat=excluded.heartbeat,site=excluded.site,region=excluded.region',(app.state.gateway.cluster.node,store.clock(),app.state.gateway.cluster.topology.site,app.state.gateway.cluster.topology.region))
            store.set_setting('auth_epoch',secrets.token_hex(16))
            ctx['oauth']._clients.clear()
            ctx['oauth']._registry.clear()
            store.audit(p["id"],"backup.restored","workspace")
            return result
        finally:
            app.state.gateway.generation+=1
            app.state.gateway.maintenance=False

    @app.post("/api/v1/account/password",tags=["Identity"])
    def change_password(body:PasswordChange,p=Depends(user)):
        row=store.one("SELECT * FROM users WHERE id=?",(p["id"],))
        if row["issuer"]!="local" or not verify_password(body.current_password,row["password_hash"]):raise HTTPException(403,"Current local password is incorrect")
        with store.transaction():
            store.update("users",p["id"],{"password_hash":hash_password(body.new_password)})
            store.run("DELETE FROM sessions WHERE user_id=?",(p["id"],))
            store.audit(p["id"],"password.changed",p["id"])
        return {"ok":True,"sign_in_required":True}

    @app.post("/api/v1/users/{record_id}/revoke-sessions",tags=["Identity"])
    def revoke_sessions(record_id:str,p=Depends(admin)):
        store.run("DELETE FROM sessions WHERE user_id=?",(record_id,))
        store.audit(p["id"],"sessions.revoked",record_id)
        return {"ok":True}

    @app.post("/api/v1/monitoring-token",tags=["Operations"])
    def monitoring_token(p=Depends(admin)):
        token="plq_metrics_"+secrets.token_urlsafe(32)
        store.set_setting("metrics_token_hash",digest(token))
        store.audit(p["id"],"monitoring_token.rotated","workspace")
        return {"token":token}

    @app.get("/metrics",tags=["Operations"])
    def metrics(request:Request):
        authorization=request.headers.get("authorization","")
        configured=store.setting("metrics_token_hash","")
        token=bearer_token(authorization) or ''
        if not configured or not secrets.compare_digest(digest(token),configured):raise HTTPException(401,"A monitoring token is required")
        lines=["# TYPE pliqra_requests_total counter"]
        for row in store.all("SELECT status,COUNT(*) AS n FROM events GROUP BY status"):
            lines.append('pliqra_requests_total{status="'+row["status"]+'"} '+str(row["n"]))
        lines.extend(["# TYPE pliqra_active_requests gauge","pliqra_active_requests "+str(app.state.gateway.active),"# TYPE pliqra_unresolved_costs gauge","pliqra_unresolved_costs "+str(store.one("SELECT COUNT(*) AS n FROM events WHERE reserved_micros>0 AND status!='pending'")["n"])])
        return Response("\n".join(lines)+"\n",media_type="text/plain; version=0.0.4")
