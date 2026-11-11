import base64
import json
import re
import secrets
import zlib
from datetime import datetime, timezone
from urllib.parse import urlsplit, parse_qs

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from onelogin.saml2.auth import OneLogin_Saml2_Auth
from onelogin.saml2.settings import OneLogin_Saml2_Settings
from onelogin.saml2.logout_request import OneLogin_Saml2_Logout_Request
from pydantic import BaseModel, Field
from typing import Annotated, Literal

from .access import sync_group_memberships
from .db import encode, now, uid
from .security import digest, endpoint_url, bearer_token

USER_SCHEMA="urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA="urn:ietf:params:scim:schemas:core:2.0:Group"
ENTERPRISE_SCHEMA='urn:ietf:params:scim:schemas:extension:enterprise:2.0:User'
ENTERPRISE_FIELDS={'employeeNumber','costCenter','organization','division','department','manager'}
LIST_SCHEMA="urn:ietf:params:scim:api:messages:2.0:ListResponse"


class SamlInput(BaseModel):
    name: Annotated[str,Field(min_length=1,max_length=100)]
    entity_id: Annotated[str,Field(min_length=1,max_length=2000)]
    sso_url: Annotated[str,Field(min_length=8,max_length=2000)]
    certificate: Annotated[str,Field(min_length=100,max_length=20000)]
    email_attribute: str="email"
    name_attribute: str="displayName"
    groups_attribute: str="groups"
    slo_url:str=''


class IdentityConfig(BaseModel):
    name: Annotated[str,Field(min_length=1,max_length=100)]
    active: bool=True
    client_secret: Annotated[str|None,Field(max_length=4096)]=None
    subject_claim: Annotated[str,Field(min_length=1,max_length=100)]="sub"
    groups_claim: Annotated[str,Field(min_length=1,max_length=100)]="groups"
    certificate: Annotated[str|None,Field(max_length=20000)]=None
    slo_url:Annotated[str|None,Field(max_length=2000)]=None


class GroupRule(BaseModel):
    provider_id:str
    group_name:Annotated[str,Field(min_length=1,max_length=200)]
    project_id:str
    role:Literal["member","manager"]="member"


def federated_account(store,provider,info):
    config=json.loads(provider.get("config") or "{}")
    claim=config.get("subject_claim","sub")
    subject=info.get(claim)
    if not isinstance(subject,str) or not subject or len(subject)>512:
        raise HTTPException(403,"Identity provider did not return the configured stable subject")
    with store.transaction():
        account=store.one("SELECT * FROM users WHERE issuer=? AND subject=?",(provider["id"],subject))
        if config.get("scim_enabled") and (not account or not account["external_id"]):
            raise HTTPException(403,"This identity must be provisioned through SCIM before signing in")
        if not account:
            account={"id":uid("usr_"),"email":str(info.get("email",""))[:254],"name":str(info.get("name",subject))[:100],"role":"viewer","issuer":provider["id"],"subject":subject,"active":1,"created":now()}
            store.insert("users",account)
        if not account["active"]:raise HTTPException(403,"Your account has been disabled")
        claimed=info.get(config.get("groups_claim","groups"),[])
        if not isinstance(claimed,list):claimed=[]
        group_names={str(x)[:200] for x in claimed[:200] if isinstance(x,str)}
        for group in store.all("SELECT name,members FROM identity_groups WHERE provider_id=?",(provider["id"],)):
            if account["id"] in json.loads(group["members"]):group_names.add(group["name"])
        store.update("users",account["id"],{"groups":encode(sorted(group_names))})
        sync_group_memberships(store,provider["id"],account["id"],group_names)
        store.audit(account["id"],"session.federated_login",provider["id"])
    return account


def mount(app):
    ctx=app.state.context
    store,vault,admin=ctx["store"],ctx["vault"],ctx["admin"]
    saml_cookie='plq_saml_'+ctx['organization']['id'] if ctx.get('organization') else 'plq_saml'
    saml_path=ctx.get('base_path','')+'/auth/saml/'
    slo_cookie=saml_cookie+'_logout'

    @app.post("/api/v1/identity-providers/saml",tags=["Identity"],status_code=201)
    def add_saml(body:SamlInput,p=Depends(admin)):
        from cryptography import x509
        from cryptography.hazmat.primitives.serialization import Encoding
        try:
            certificate=x509.load_pem_x509_certificate(body.certificate.encode())
            if certificate.not_valid_after_utc <= datetime.now(timezone.utc):raise ValueError()
            pem=certificate.public_bytes(Encoding.PEM).decode()
        except ValueError:raise HTTPException(422,"Enter a valid, unexpired PEM signing certificate")
        endpoint_url(body.sso_url)
        if body.slo_url and not endpoint_url(body.slo_url).startswith('https://'):raise HTTPException(422,'SAML logout requires HTTPS')
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.x509.oid import NameOID
        from datetime import timedelta
        private=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,"Pliqra SAML")])
        sp_cert=x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(private.public_key()).serial_number(x509.random_serial_number()).not_valid_before(datetime.now(timezone.utc)-timedelta(minutes=5)).not_valid_after(datetime.now(timezone.utc)+timedelta(days=3650)).sign(private,hashes.SHA256())
        config={"sso_url":body.sso_url,"certificate":pem,"email_attribute":body.email_attribute,"name_attribute":body.name_attribute,"groups_attribute":body.groups_attribute,"subject_claim":"sub","groups_claim":"groups","sp_certificate":sp_cert.public_bytes(Encoding.PEM).decode(),"sp_private_key":vault.encrypt(private.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode())}
        record_id=uid("idp_")
        config['slo_url']=body.slo_url
        store.insert("identity_providers",{"id":record_id,"name":body.name,"issuer":body.entity_id,"client_id":"","secret":"","default_role":"viewer","protocol":"saml","config":encode(config),"created":now()})
        store.audit(p["id"],"identity.saml_created",record_id)
        return {"id":record_id,"metadata_path":f"/auth/saml/{record_id}/metadata","acs_path":f"/auth/saml/{record_id}/acs"}

    @app.patch("/api/v1/identity-providers/{record_id}",tags=["Identity"])
    def edit_identity(record_id:str,body:IdentityConfig,p=Depends(admin)):
        row=store.one("SELECT * FROM identity_providers WHERE id=?",(record_id,))
        if not row:raise HTTPException(404,"Identity provider not found")
        config=json.loads(row["config"])
        config.update(subject_claim=body.subject_claim,groups_claim=body.groups_claim)
        if body.slo_url is not None:
            if row['protocol']!='saml':raise HTTPException(422,'SAML logout is only available for SAML providers')
            if body.slo_url and not endpoint_url(body.slo_url).startswith('https://'):raise HTTPException(422,'SAML logout requires HTTPS')
            config['slo_url']=body.slo_url
        if body.certificate is not None:
            from cryptography import x509
            try:x509.load_pem_x509_certificate(body.certificate.encode())
            except ValueError:raise HTTPException(422,"Invalid PEM certificate")
            config["certificate"]=body.certificate
        fields={"name":body.name,"active":int(body.active),"config":encode(config)}
        if body.client_secret is not None:fields["secret"]=vault.encrypt(body.client_secret)
        with store.transaction():
            store.update("identity_providers",record_id,fields)
            store.run("DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE issuer=?)",(record_id,))
            store.run("DELETE FROM saml_flows WHERE provider_id=?",(record_id,))
            # Authlib caches provider clients; reconstruct after credential changes.
            ctx["oauth"]._clients.pop("oidc_"+record_id,None)
            ctx["oauth"]._registry.pop("oidc_"+record_id,None)
            store.audit(p["id"],"identity.updated",record_id,{"name":body.name,"active":body.active,"credentials_rotated":body.client_secret is not None})
        return {"ok":True}

    @app.get("/api/v1/group-rules",tags=["Identity"])
    def group_rules(p=Depends(admin)):return store.all("SELECT * FROM group_rules")

    @app.post("/api/v1/group-rules",tags=["Identity"],status_code=201)
    def create_rule(body:GroupRule,p=Depends(admin)):
        if not store.one("SELECT id FROM identity_providers WHERE id=?",(body.provider_id,)) or not store.one("SELECT id FROM projects WHERE id=?",(body.project_id,)):raise HTTPException(422,"Unknown provider or project")
        record_id=uid("rule_")
        with store.transaction():
            store.insert("group_rules",{"id":record_id,**body.model_dump()})
            for account in store.all("SELECT * FROM users WHERE issuer=?",(body.provider_id,)):
                sync_group_memberships(store,body.provider_id,account["id"],json.loads(account["groups"]))
            store.audit(p["id"],"group_rule.created",record_id,body.model_dump())
        return {"id":record_id}

    @app.delete("/api/v1/group-rules/{record_id}",tags=["Identity"])
    def delete_rule(record_id:str,p=Depends(admin)):
        row=store.one("SELECT * FROM group_rules WHERE id=?",(record_id,))
        if not row:raise HTTPException(404,"Rule not found")
        with store.transaction():
            store.run("DELETE FROM group_rules WHERE id=?",(record_id,))
            for account in store.all("SELECT * FROM users WHERE issuer=?",(row["provider_id"],)):
                sync_group_memberships(store,row["provider_id"],account["id"],json.loads(account["groups"]))
            store.audit(p["id"],"group_rule.deleted",record_id)
        return {"ok":True}

    def saml_provider(record_id):
        row=store.one("SELECT * FROM identity_providers WHERE id=? AND active=1 AND protocol='saml'",(record_id,))
        if not row:raise HTTPException(404,"SAML provider not found")
        return row,json.loads(row["config"])

    def saml_settings(request,record_id):
        row,config=saml_provider(record_id)
        origin=ctx["public_url"] or str(request.base_url).rstrip("/")
        if urlsplit(origin).scheme != 'https':
            raise HTTPException(422,'Configure an HTTPS public URL before using SAML sign-in')
        settings={"strict":True,"debug":False,"sp":{"entityId":origin+f"/auth/saml/{record_id}/metadata","assertionConsumerService":{"url":origin+f"/auth/saml/{record_id}/acs","binding":"urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"}},"idp":{"entityId":row["issuer"],"singleSignOnService":{"url":config["sso_url"],"binding":"urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"},"x509cert":config["certificate"]},"security":{"wantAssertionsSigned":True,"wantNameId":True,"rejectUnsolicitedResponsesWithInResponseTo":True,"requestedAuthnContext":False,"signatureAlgorithm":"http://www.w3.org/2001/04/xmldsig-more#rsa-sha256","digestAlgorithm":"http://www.w3.org/2001/04/xmlenc#sha256"}}
        if config.get("sp_private_key"):
            settings["sp"].update(x509cert=config["sp_certificate"],privateKey=vault.decrypt(config["sp_private_key"]))
            settings["security"]["authnRequestsSigned"]=True
        if config.get('slo_url'):
            settings['sp']['singleLogoutService']={'url':origin+f'/auth/saml/{record_id}/sls','binding':'urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect'}
            settings['idp']['singleLogoutService']={'url':config['slo_url'],'binding':'urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect'}
            settings['security'].update(logoutRequestSigned=True,logoutResponseSigned=True)
        settings["security"]["rejectDeprecatedAlgorithm"]=True
        if urlsplit(origin).hostname in ('localhost','127.0.0.1','::1'):
            settings['security']['allowSingleLabelDomains']=True
        return row,config,settings

    def saml_request(request,post=None):
        origin=urlsplit(ctx["public_url"] or str(request.base_url))
        return {"https":"on" if origin.scheme=="https" else "off","http_host":origin.netloc,"server_port":str(origin.port or (443 if origin.scheme=="https" else 80)),"script_name":request.url.path,"get_data":dict(request.query_params),"post_data":post or {}}

    @app.get("/auth/saml/{record_id}/metadata",tags=["Identity"])
    def saml_metadata(record_id:str,request:Request):
        _,_,settings=saml_settings(request,record_id)
        toolkit=OneLogin_Saml2_Settings(settings,sp_validation_only=True)
        return Response(toolkit.get_sp_metadata(),media_type="application/samlmetadata+xml")

    @app.get("/auth/saml/{record_id}/login",tags=["Identity"])
    def saml_login(record_id:str,request:Request):
        _,_,settings=saml_settings(request,record_id)
        toolkit=OneLogin_Saml2_Auth(saml_request(request),old_settings=settings)
        relay=secrets.token_urlsafe(32)
        target=toolkit.login(return_to=relay)
        store.insert("saml_flows",{"id":digest(relay),"provider_id":record_id,"request_id":toolkit.get_last_request_id(),"expires":now()+300})
        response=RedirectResponse(target,302)
        response.set_cookie(saml_cookie,relay,httponly=True,secure=True,samesite="none",max_age=300,path=saml_path)
        return response

    @app.post("/auth/saml/{record_id}/acs",tags=["Identity"])
    async def saml_acs(record_id:str,request:Request):
        provider,config,settings=saml_settings(request,record_id)
        raw=(await request.body()).decode()
        values={key:entries[0] for key,entries in parse_qs(raw).items()}
        relay=values.get("RelayState","")
        if not relay or not secrets.compare_digest(request.cookies.get(saml_cookie,""),relay):raise HTTPException(403,"SAML browser state is missing or invalid; use HTTPS and start sign-in again")
        flow=store.one("SELECT * FROM saml_flows WHERE id=? AND provider_id=? AND kind='login' AND expires>?",(digest(relay),record_id,now()))
        if not flow:raise HTTPException(403,"SAML sign-in expired or was already used")
        toolkit=OneLogin_Saml2_Auth(saml_request(request,values),old_settings=settings)
        try:
            toolkit.process_response(request_id=flow["request_id"])
            if toolkit.get_errors() or not toolkit.is_authenticated():raise ValueError()
            assertion=toolkit.get_last_assertion_id()
            if not assertion:raise ValueError()
            attributes=toolkit.get_attributes()
            name_id=toolkit.get_nameid()
            if not name_id:raise ValueError()
        except Exception:
            store.audit("anonymous","saml.validation_failed",record_id)
            raise HTTPException(403,"SAML response could not be validated")
        with store.transaction():
            if not store.one("SELECT id FROM saml_flows WHERE id=?",(digest(relay),)) or store.one("SELECT id FROM saml_assertions WHERE id=?",(assertion,)):raise HTTPException(403,"SAML response was already used")
            store.run("DELETE FROM saml_flows WHERE id=?",(digest(relay),))
            store.insert("saml_assertions",{"id":assertion,"expires":max(now()+3600,toolkit.get_last_assertion_not_on_or_after() or 0)})
        def first(attribute,fallback=""):
            value=attributes.get(attribute,[])
            return value[0] if value else fallback
        account=federated_account(store,provider,{"sub":name_id,"email":first(config.get("email_attribute","email")),"name":first(config.get("name_attribute","displayName"),name_id),"groups":attributes.get(config.get("groups_attribute","groups"),[])})
        response=ctx["session_response"](account,request,{'name_id':name_id,'name_id_format':toolkit.get_nameid_format(),'session_index':toolkit.get_session_index()})
        response.delete_cookie(saml_cookie,path=saml_path)
        return response

    @app.post('/api/v1/identity-providers/{record_id}/logout',tags=['Identity'])
    def saml_logout(record_id:str,request:Request,p=Depends(ctx['user'])):
        if p['user']['issuer']!=record_id:raise HTTPException(403,'Use the provider that authenticated this session')
        _,config,settings=saml_settings(request,record_id)
        if not config.get('slo_url'):raise HTTPException(422,'Configure the provider logout URL first')
        row=store.one('SELECT federation_context FROM sessions WHERE token_hash=?',(p['session_hash'],))
        context=json.loads(row['federation_context'])
        if not context.get('name_id'):raise HTTPException(409,'Sign in again to establish a logout-capable SAML session')
        toolkit=OneLogin_Saml2_Auth(saml_request(request),old_settings=settings)
        relay=secrets.token_urlsafe(32)
        target=toolkit.logout(return_to=relay,name_id=context['name_id'],session_index=context.get('session_index'),name_id_format=context.get('name_id_format'))
        with store.transaction():
            store.insert('saml_flows',{'id':digest(relay),'provider_id':record_id,'request_id':toolkit.get_last_request_id(),'expires':now()+300,'kind':'logout','user_id':p['id']})
            store.run('DELETE FROM sessions WHERE token_hash=?',(p['session_hash'],))
            store.audit(p['id'],'session.saml_logout_started',record_id)
        response=JSONResponse({'redirect_url':target})
        response.set_cookie(slo_cookie,relay,httponly=True,secure=True,samesite='lax',max_age=300,path=saml_path)
        response.delete_cookie(ctx['cookie_name'],path=ctx.get('base_path') or '/')
        return response

    @app.get('/auth/saml/{record_id}/sls',tags=['Identity'])
    def saml_sls(record_id:str,request:Request):
        _,config,settings=saml_settings(request,record_id)
        if not config.get('slo_url'):raise HTTPException(404,'SAML logout is not configured')
        query=dict(request.query_params)
        if any(len(value)>100000 for value in query.values()) or not query.get('Signature'):raise HTTPException(403,'A signed SAML logout message is required')
        try:
            encoded=query.get('SAMLResponse') or query.get('SAMLRequest')
            inflater=zlib.decompressobj(-15)
            xml=inflater.decompress(base64.b64decode(encoded,validate=True),256001)
            if len(xml)>256000 or not inflater.eof or inflater.unconsumed_tail:raise ValueError()
            from lxml import etree
            root=etree.fromstring(xml,etree.XMLParser(resolve_entities=False,no_network=True))
            stamp=datetime.fromisoformat(root.get('IssueInstant','').replace('Z','+00:00'))
            if stamp.tzinfo is None or abs(now()-stamp.timestamp())>300:raise ValueError()
        except Exception:raise HTTPException(403,'SAML logout is oversized, malformed or outside its freshness window')
        flow=None
        if query.get('SAMLResponse'):
            relay=query.get('RelayState','')
            if not relay or not secrets.compare_digest(request.cookies.get(slo_cookie,''),relay):raise HTTPException(403,'SAML logout browser state is invalid')
            flow=store.one("SELECT * FROM saml_flows WHERE id=? AND provider_id=? AND kind='logout' AND expires>?",(digest(relay),record_id,now()))
            if not flow:raise HTTPException(403,'SAML logout expired or was already completed')
        settings['security']['wantMessagesSigned']=True
        toolkit=OneLogin_Saml2_Auth(saml_request(request),old_settings=settings)
        try:
            target=toolkit.process_slo(keep_local_session=True,request_id=flow['request_id'] if flow else None)
            if toolkit.get_errors():raise ValueError()
            message=toolkit.get_last_message_id()
            if not message:raise ValueError()
            name_id=None;indexes=[]
            if query.get('SAMLRequest'):
                xml=toolkit.get_last_request_xml()
                name_id=OneLogin_Saml2_Logout_Request.get_nameid_data(xml,settings['sp'].get('privateKey'))['Value']
                indexes=OneLogin_Saml2_Logout_Request.get_session_indexes(xml)
        except Exception:
            store.audit('anonymous','saml.logout_validation_failed',record_id)
            raise HTTPException(403,'SAML logout signature or message validation failed')
        with store.transaction():
            replay='slo:'+message
            if store.one('SELECT id FROM saml_assertions WHERE id=?',(replay,)):raise HTTPException(403,'SAML logout was already processed')
            if flow and not store.one('SELECT id FROM saml_flows WHERE id=?',(flow['id'],)):raise HTTPException(403,'SAML logout was already processed')
            store.insert('saml_assertions',{'id':replay,'expires':now()+86400})
            if flow:
                store.run('DELETE FROM saml_flows WHERE id=?',(flow['id'],))
                store.run('DELETE FROM sessions WHERE user_id=?',(flow['user_id'],))
            else:
                sessions=store.all('SELECT s.token_hash,s.federation_context FROM sessions s JOIN users u ON u.id=s.user_id WHERE u.issuer=? AND u.subject=?',(record_id,name_id))
                for session in sessions:
                    if not indexes or json.loads(session['federation_context']).get('session_index') in indexes:
                        store.run('DELETE FROM sessions WHERE token_hash=?',(session['token_hash'],))
            store.audit('federation','session.saml_logout_completed',record_id)
        response=RedirectResponse(target or ctx.get('base_path','')+'/',302)
        response.delete_cookie(slo_cookie,path=saml_path)
        response.delete_cookie(ctx['cookie_name'],path=ctx.get('base_path') or '/')
        return response

    @app.post("/api/v1/identity-providers/{record_id}/scim-token",tags=["Identity"])
    def scim_token(record_id:str,p=Depends(admin)):
        provider=store.one("SELECT * FROM identity_providers WHERE id=?",(record_id,))
        if not provider:raise HTTPException(404,"Identity provider not found")
        token="plq_scim_"+secrets.token_urlsafe(32)
        config=json.loads(provider["config"])
        config["scim_enabled"]=True
        with store.transaction():
            store.run("UPDATE scim_tokens SET active=0 WHERE provider_id=?",(record_id,))
            store.insert("scim_tokens",{"id":uid("scim_"),"provider_id":record_id,"token_hash":digest(token),"created":now()})
            store.update("identity_providers",record_id,{"config":encode(config)})
            store.run("DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE issuer=?)",(record_id,))
            store.audit(p["id"],"scim.token_rotated",record_id)
        return {"token":token,"base_path":f"/scim/v2/{record_id}","subject_mapping":"SCIM externalId must match the configured SSO subject claim. Provisioning is required after enabling SCIM."}

    @app.delete("/api/v1/identity-providers/{record_id}/scim-token",tags=["Identity"])
    def revoke_scim(record_id:str,p=Depends(admin)):
        store.run("UPDATE scim_tokens SET active=0 WHERE provider_id=?",(record_id,))
        store.audit(p["id"],"scim.token_revoked",record_id)
        return {"ok":True}

    def scim_auth(provider_id,request):
        token=bearer_token(request.headers.get("authorization","")) or ''
        row=store.one("SELECT t.id FROM scim_tokens t JOIN identity_providers i ON i.id=t.provider_id WHERE t.provider_id=? AND t.token_hash=? AND t.active=1 AND i.active=1",(provider_id,digest(token)))
        if not row:raise HTTPException(401,"Invalid SCIM provisioning token")

    def scim_json(payload,status=200):return JSONResponse(payload,status_code=status,media_type="application/scim+json")

    def scim_error(message,status=400,kind="invalidValue"):
        return scim_json({"schemas":["urn:ietf:params:scim:api:messages:2.0:Error"],"status":str(status),"scimType":kind,"detail":message},status)

    @app.get("/scim/v2/{provider_id}/ServiceProviderConfig",tags=["Provisioning"])
    def provider_config(provider_id:str,request:Request):
        scim_auth(provider_id,request)
        return scim_json({"schemas":["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],"patch":{"supported":True},"bulk":{"supported":False,"maxOperations":0,"maxPayloadSize":0},"filter":{"supported":True,"maxResults":1000},"changePassword":{"supported":False},"sort":{"supported":False},"etag":{"supported":False},"authenticationSchemes":[{"type":"oauthbearertoken","name":"Scoped provisioning token","description":"Provider-scoped bearer token","primary":True}]})

    def render_scim_user(row):
        enterprise=json.loads(row.get('enterprise') or '{}')
        return {"schemas":[USER_SCHEMA]+([ENTERPRISE_SCHEMA] if enterprise else []),"id":row["id"],"externalId":row["external_id"],"userName":row["username"] or row["subject"],"displayName":row["name"],"name":{"formatted":row["name"]},"emails":[{"value":row["email"],"primary":True}],"active":bool(row["active"]),"meta":{"resourceType":"User"},**({ENTERPRISE_SCHEMA:enterprise} if enterprise else {})}

    def render_scim_group(row):
        return {"schemas":[GROUP_SCHEMA],"id":row["id"],"externalId":row["external_id"],"displayName":row["name"],"members":[{"value":value} for value in json.loads(row["members"])],"meta":{"resourceType":"Group"}}

    def sync_scim_groups(provider_id):
        groups=store.all("SELECT * FROM identity_groups WHERE provider_id=?",(provider_id,))
        for account in store.all("SELECT id FROM users WHERE issuer=?",(provider_id,)):
            names=[group["name"] for group in groups if account["id"] in json.loads(group["members"])]
            store.update("users",account["id"],{"groups":encode(names)})
            sync_group_memberships(store,provider_id,account["id"],names)

    def discovery_resources(resource):
        schema_schema="urn:ietf:params:scim:schemas:core:2.0:Schema"
        type_schema="urn:ietf:params:scim:schemas:core:2.0:ResourceType"
        if resource=='ResourceTypes':
            return [{'schemas':[type_schema],'id':name,'name':name,'endpoint':'/'+name+'s','schema':schema,'schemaExtensions':[{'schema':ENTERPRISE_SCHEMA,'required':False}] if name=='User' else [],'meta':{'resourceType':'ResourceType'}} for name,schema in [('User',USER_SCHEMA),('Group',GROUP_SCHEMA)]]
        def attr(name,kind='string',multi=False,required=False,sub=None):
            value={'name':name,'type':kind,'multiValued':multi,'required':required,'caseExact':False,'mutability':'readWrite','returned':'default','uniqueness':'none'}
            if sub:value['subAttributes']=sub
            return value
        common=[attr('externalId')]
        return [{'schemas':[schema_schema],'id':USER_SCHEMA,'name':'User','attributes':common+[attr('userName',required=True),attr('displayName'),attr('active','boolean'),attr('name','complex',sub=[attr('formatted'),attr('givenName'),attr('familyName')]),attr('emails','complex',True,sub=[attr('value'),attr('primary','boolean'),attr('type')])],'meta':{'resourceType':'Schema'}}, {'schemas':[schema_schema],'id':GROUP_SCHEMA,'name':'Group','attributes':common+[attr('displayName',required=True),attr('members','complex',True,sub=[attr('value'),attr('display')])],'meta':{'resourceType':'Schema'}},{'schemas':[schema_schema],'id':ENTERPRISE_SCHEMA,'name':'EnterpriseUser','attributes':[attr(name) for name in sorted(ENTERPRISE_FIELDS-{'manager'})]+[attr('manager','complex',sub=[attr('value'),attr('$ref'),attr('displayName')])],'meta':{'resourceType':'Schema'}}]

    @app.get("/scim/v2/{provider_id}/{resource}",tags=["Provisioning"])
    def scim_list(provider_id:str,resource:str,request:Request,startIndex:int=1,count:int=100,filter:str=""):
        scim_auth(provider_id,request)
        if resource in ('Schemas','ResourceTypes'):
            items=discovery_resources(resource)
            return scim_json({'schemas':[LIST_SCHEMA],'totalResults':len(items),'startIndex':1,'itemsPerPage':len(items),'Resources':items})
        if resource not in ("Users","Groups"):return scim_error("Resource not supported",404)
        table,owner=("users","issuer") if resource=="Users" else ("identity_groups","provider_id")
        where,args=owner+"=?",[provider_id]
        if resource=="Users":where+=" AND external_id IS NOT NULL"
        if filter:
            match=re.fullmatch(r'(userName|externalId|displayName)\s+eq\s+"([^"\\]{0,512})"',filter)
            if not match:return scim_error("Only equality filters for userName, externalId and displayName are supported",400,"invalidFilter")
            column={"userName":"username","externalId":"external_id","displayName":"name"}[match[1]]
            if resource=="Groups" and column=="username":return scim_error("userName is not a group attribute",400,"invalidFilter")
            where+=" AND "+column+"=?";args.append(match[2])
        total=store.one(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}",args)["n"]
        count=max(0,min(count,1000));startIndex=max(1,startIndex)
        rows=store.all(f"SELECT * FROM {table} WHERE {where} ORDER BY id LIMIT ? OFFSET ?",[*args,count,startIndex-1])
        render=render_scim_user if resource=="Users" else render_scim_group
        return scim_json({"schemas":[LIST_SCHEMA],"totalResults":total,"startIndex":startIndex,"itemsPerPage":len(rows),"Resources":[render(row) for row in rows]})

    @app.get("/scim/v2/{provider_id}/{resource}/{record_id}",tags=["Provisioning"])
    def scim_get(provider_id:str,resource:str,record_id:str,request:Request):
        scim_auth(provider_id,request)
        if resource in ('Schemas','ResourceTypes'):
            item=next((v for v in discovery_resources(resource) if v['id']==record_id),None)
            return scim_json(item) if item else scim_error('Resource not found',404)
        if resource not in ("Users","Groups"):return scim_error("Resource not supported",404)
        table,owner=("users","issuer") if resource=="Users" else ("identity_groups","provider_id")
        row=store.one(f"SELECT * FROM {table} WHERE id=? AND {owner}=?",(record_id,provider_id))
        if not row:return scim_error("Resource not found",404)
        return scim_json(render_scim_user(row) if resource=="Users" else render_scim_group(row))

    def user_fields(body):
        subject=body.get("externalId") or body.get("userName")
        if not isinstance(subject,str) or not subject or len(subject)>512:raise ValueError("A stable externalId or userName is required")
        emails=body.get("emails") or []
        email=next((item.get("value","") for item in emails if isinstance(item,dict) and item.get("primary")),"") or (emails[0].get("value","") if emails and isinstance(emails[0],dict) else "")
        active=body.get("active",True)
        if not isinstance(active,bool):raise ValueError("active must be a boolean")
        enterprise=body.get(ENTERPRISE_SCHEMA,{})
        if not isinstance(enterprise,dict) or set(enterprise)-ENTERPRISE_FIELDS:raise ValueError('Unsupported enterprise user attribute')
        for key,value in enterprise.items():
            if key=='manager':
                if not isinstance(value,dict) or set(value)-{'value','$ref','displayName'} or any(not isinstance(v,str) or len(v)>512 for v in value.values()):raise ValueError('Invalid enterprise manager')
            elif not isinstance(value,str) or len(value)>512:raise ValueError('Invalid enterprise user attribute')
        return {"subject":subject,"username":str(body.get("userName") or subject)[:512],"external_id":str(body.get("externalId") or subject),"email":str(email)[:254],"name":str(body.get("displayName") or body.get("name",{}).get("formatted") or body.get("userName") or subject)[:100],"active":int(active),'enterprise':encode(enterprise)}

    def group_fields(body,provider_id):
        name=body.get("displayName")
        if not isinstance(name,str) or not name or len(name)>200:raise ValueError("displayName is required")
        members=body.get("members",[])
        if not isinstance(members,list) or len(members)>10000:raise ValueError("Invalid group members")
        ids=[]
        for member in members:
            value=member.get("value") if isinstance(member,dict) else None
            if not value or not store.one("SELECT id FROM users WHERE id=? AND issuer=?",(value,provider_id)):raise ValueError("Every member must be a user provisioned by this provider")
            ids.append(value)
        return {"name":name,"external_id":body.get("externalId"),"members":encode(sorted(set(ids)))}

    @app.post("/scim/v2/{provider_id}/{resource}",tags=["Provisioning"])
    async def scim_create(provider_id:str,resource:str,request:Request):
        scim_auth(provider_id,request)
        body=await request.json()
        try:
            with store.transaction():
                record_id=uid("usr_" if resource=="Users" else "grp_")
                if resource=="Users":
                    fields=user_fields(body)
                    if store.one("SELECT id FROM users WHERE issuer=? AND subject=?",(provider_id,fields["subject"])):return scim_error("User already exists",409,"uniqueness")
                    store.insert("users",{"id":record_id,"issuer":provider_id,"role":"viewer",**fields,"created":now()})
                    row=store.one("SELECT * FROM users WHERE id=?",(record_id,));payload=render_scim_user(row)
                elif resource=="Groups":
                    store.insert("identity_groups",{"id":record_id,"provider_id":provider_id,**group_fields(body,provider_id)})
                    sync_scim_groups(provider_id)
                    payload=render_scim_group(store.one("SELECT * FROM identity_groups WHERE id=?",(record_id,)))
                else:return scim_error("Resource not supported",404)
                store.audit("scim:"+provider_id,"scim.created",record_id)
        except (ValueError,TypeError,AttributeError) as exc:return scim_error(str(exc))
        return scim_json(payload,201)

    @app.api_route("/scim/v2/{provider_id}/{resource}/{record_id}",methods=["PATCH","PUT"],tags=["Provisioning"])
    async def scim_update(provider_id:str,resource:str,record_id:str,request:Request):
        scim_auth(provider_id,request)
        if resource not in ("Users","Groups"):return scim_error("Resource not supported",404)
        table,owner=("users","issuer") if resource=="Users" else ("identity_groups","provider_id")
        row=store.one(f"SELECT * FROM {table} WHERE id=? AND {owner}=?",(record_id,provider_id))
        if not row:return scim_error("Resource not found",404)
        body=await request.json()
        current=render_scim_user(row) if resource=="Users" else render_scim_group(row)
        try:
            if request.method=="PUT":current=body
            else:
                operations=body.get("Operations",[])
                if not isinstance(operations,list) or len(operations)>100:raise ValueError("Invalid patch operations")
                for op in operations:
                    operation=str(op.get("op","")).lower();path=op.get("path","");value=op.get("value")
                    if operation not in ("add","replace","remove"):raise ValueError("Unsupported patch operation")
                    if not path and operation in ("add","replace") and isinstance(value,dict):current.update(value)
                    elif path in ("active","displayName","userName","externalId","emails","name"):
                        if operation=="remove":current.pop(path,None)
                        else:current[path]=value
                    elif resource=='Users' and (path==ENTERPRISE_SCHEMA or path.startswith(ENTERPRISE_SCHEMA+':')):
                        if path==ENTERPRISE_SCHEMA:
                            if operation=='remove':current.pop(ENTERPRISE_SCHEMA,None)
                            else:current[ENTERPRISE_SCHEMA]=value
                        else:
                            parts=path[len(ENTERPRISE_SCHEMA)+1:].split('.')
                            if len(parts)>2 or parts[0] not in ENTERPRISE_FIELDS or len(parts)==2 and parts[0]!='manager':raise ValueError('Unsupported enterprise path')
                            extension=current.setdefault(ENTERPRISE_SCHEMA,{})
                            if len(parts)==2:
                                target=extension.setdefault('manager',{});key=parts[1]
                            else:target=extension;key=parts[0]
                            if operation=='remove':target.pop(key,None)
                            else:target[key]=value
                    elif resource=="Groups" and path=="members":
                        if operation=="remove":current["members"]=[]
                        elif operation=="replace":current["members"]=value
                        else:current["members"]=current.get("members",[])+value
                    elif resource=="Groups" and operation=="remove" and re.fullmatch(r'members\[value eq "[^"\\]+"\]',path):
                        target=path.split('"')[1];current["members"]=[member for member in current.get("members",[]) if member.get("value")!=target]
                    else:raise ValueError("Unsupported SCIM attribute path")
            with store.transaction():
                fields=user_fields(current) if resource=="Users" else group_fields(current,provider_id)
                store.update(table,record_id,fields)
                if resource=="Users" and not fields["active"]:store.run("DELETE FROM sessions WHERE user_id=?",(record_id,))
                sync_scim_groups(provider_id)
                store.audit("scim:"+provider_id,"scim.updated",record_id)
        except (ValueError,TypeError,AttributeError) as exc:return scim_error(str(exc))
        return scim_get(provider_id,resource,record_id,request)

    @app.delete("/scim/v2/{provider_id}/{resource}/{record_id}",tags=["Provisioning"])
    def scim_delete(provider_id:str,resource:str,record_id:str,request:Request):
        scim_auth(provider_id,request)
        with store.transaction():
            if resource=="Users":
                row=store.one("SELECT id FROM users WHERE id=? AND issuer=?",(record_id,provider_id))
                if not row:return scim_error("User not found",404)
                store.update("users",record_id,{"active":0})
                store.run("DELETE FROM sessions WHERE user_id=?",(record_id,))
            elif resource=="Groups":
                store.run("DELETE FROM identity_groups WHERE id=? AND provider_id=?",(record_id,provider_id))
                sync_scim_groups(provider_id)
            else:return scim_error("Resource not supported",404)
            store.audit("scim:"+provider_id,"scim.deprovisioned",record_id)
        return Response(status_code=204)
