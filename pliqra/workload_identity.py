"""Exchange explicitly trusted workload JWTs for short-lived scoped Pliqra keys."""
import hashlib
import json
import secrets
from typing import Annotated,Literal

from fastapi import Depends,HTTPException,Request
from joserfc import jwt
from joserfc.jwk import KeySet
from pydantic import BaseModel,ConfigDict,Field,model_validator

from .connectors import response_json
from .db import encode,now,uid
from .security import digest,endpoint_url


class WorkloadProvider(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name:Annotated[str,Field(min_length=1,max_length=100)]
    issuer:Annotated[str,Field(min_length=8,max_length=2000)]
    audience:Annotated[str,Field(min_length=1,max_length=500)]
    jwks_uri:Annotated[str,Field(min_length=8,max_length=2000)]
    subjects:Annotated[list[Annotated[str,Field(min_length=1,max_length=512)]],Field(min_length=1,max_length=100)]
    algorithms:Annotated[list[Literal['RS256','ES256']],Field(min_length=1,max_length=2)]=['RS256']
    project_id:str='default'
    team_id:str|None=None
    gateway_id:str|None=None
    allowed_ports:Annotated[list[str],Field(min_length=1,max_length=100)]
    ttl_seconds:Annotated[int,Field(ge=60,le=900)]=300
    rpm:Annotated[int,Field(ge=1,le=100000)]=60
    enabled:bool=True

    @model_validator(mode='after')
    def addresses(self):
        from urllib.parse import urlsplit
        self.issuer=endpoint_url(self.issuer)
        self.jwks_uri=endpoint_url(self.jwks_uri)
        if any(urlsplit(x).scheme!='https' for x in (self.issuer,self.jwks_uri)):raise ValueError('Workload issuer and JWKS require HTTPS')
        return self


class Exchange(BaseModel):
    model_config=ConfigDict(extra='forbid')
    provider_id:str
    assertion:Annotated[str,Field(min_length=20,max_length=20000)]


def mount(app):
    store=app.state.store;admin=app.state.context['admin'];cache={}

    def public(row):return {**row,'config':json.loads(row['config'])}

    @app.get('/api/v1/workload-providers',tags=['Workload identity'])
    def providers(p=Depends(admin)):
        return [public(row) for row in store.all('SELECT * FROM workload_providers ORDER BY created')]

    def save(body,p,record_id=None):
        config=body.model_dump(exclude={'name','enabled'})
        if not store.one('SELECT id FROM projects WHERE id=?',(body.project_id,)):raise HTTPException(422,'Project not found')
        if body.team_id and not store.one('SELECT 1 FROM team_projects WHERE team_id=? AND project_id=?',(body.team_id,body.project_id)):raise HTTPException(422,'Team is not assigned to this project')
        for service in body.allowed_ports:
            port=app.state.gateway.port(service)
            if port['project_id'] and port['project_id']!=body.project_id:raise HTTPException(422,'Service belongs to another project')
        config['allowed_ports']=[app.state.gateway.port(service)['id'] for service in body.allowed_ports]
        if body.gateway_id:
            profile=app.state.gateway.policies.profile(body.gateway_id)
            if profile['config']['project_id'] and profile['config']['project_id']!=body.project_id:raise HTTPException(422,'Gateway belongs to another project')
            config['gateway_id']=profile['id']
        previous=store.one('SELECT * FROM workload_providers WHERE id=?',(record_id,)) if record_id else None
        if record_id and not previous:raise HTTPException(404,'Workload provider not found')
        row={'id':record_id or uid('wi_'),'name':body.name,'config':encode(config),'enabled':int(body.enabled),'version':previous['version']+1 if previous else 1,'created':previous['created'] if previous else now()}
        with store.transaction():
            if previous:
                store.update('workload_providers',record_id,{k:v for k,v in row.items() if k!='id'})
                store.run('UPDATE api_keys SET active=0 WHERE workload_provider_id=?',(record_id,))
            else:store.insert('workload_providers',row)
            store.audit(p['id'],'workload_provider.saved',row['id'],{'version':row['version']})
        cache.pop(row['id'],None)
        return public(row)

    @app.post('/api/v1/workload-providers',tags=['Workload identity'],status_code=201)
    def create(body:WorkloadProvider,p=Depends(admin)):return save(body,p)

    @app.put('/api/v1/workload-providers/{record_id}',tags=['Workload identity'])
    def update(record_id:str,body:WorkloadProvider,p=Depends(admin)):return save(body,p,record_id)

    @app.post('/auth/workload/token',tags=['Workload identity'])
    async def exchange(body:Exchange,request:Request):
        provider=store.one('SELECT * FROM workload_providers WHERE id=? AND enabled=1',(body.provider_id,))
        if not provider:raise HTTPException(401,'Workload assertion could not be validated')
        config=json.loads(provider['config'])
        bucket=digest(body.provider_id+':'+(request.client.host if request.client else 'unknown'))
        with store.transaction():
            instant = store.clock()
            if store.one('SELECT COUNT(*) AS n FROM auth_attempts WHERE bucket=? AND created>?',(bucket,instant-60))['n']>=60:
                raise HTTPException(429,'Workload exchange limit reached',headers={'Retry-After':'60'})
            store.insert('auth_attempts',{'id':uid('auth_'),'bucket':bucket,'created':instant})
        try:
            with app.state.gateway.checking():
                cached=cache.get(provider['id'])
                if not cached or cached['expires']<=now() or cached['version']!=provider['version']:
                    async with app.state.client.stream('GET',config['jwks_uri'],timeout=5) as response:
                        response.raise_for_status()
                        jwks=await response_json(response,100000)
                    keys=jwks['keys']
                    if not isinstance(keys,list) or not keys or len(keys)>20 or any(k.get('kty') not in ('RSA','EC') or any(private in k for private in ('d','p','q','k')) for k in keys):raise ValueError()
                    cached={'keys':KeySet.import_key_set(jwks),'expires':now()+60,'version':provider['version']}
                    cache[provider['id']]=cached
                token=jwt.decode(body.assertion,cached['keys'],algorithms=config['algorithms'])
                jwt.JWTClaimsRegistry(leeway=5,iss={'essential':True,'value':config['issuer']},aud={'essential':True,'value':config['audience']},exp={'essential':True},iat={'essential':True},sub={'essential':True}).validate(token.claims)
                claims=token.claims
                if claims['sub'] not in config['subjects'] or claims['exp']-claims['iat']>3600 or now()-claims['iat']>3600:raise ValueError()
                expiry=min(now()+config['ttl_seconds'],claims['exp'])
                if expiry<=now():raise ValueError()
        except HTTPException as exc:
            if exc.status_code in (429,503):raise
            raise HTTPException(401,'Workload assertion could not be validated')
        except Exception:
            store.audit('anonymous','workload.exchange_failed',body.provider_id)
            raise HTTPException(401,'Workload assertion could not be validated')
        token_value='plq_'+secrets.token_urlsafe(32)
        assertion_id=digest(provider['id']+':'+str(claims.get('jti') or hashlib.sha256(body.assertion.encode()).hexdigest()))
        with store.transaction():
            latest=store.one('SELECT * FROM workload_providers WHERE id=? AND enabled=1',(provider['id'],))
            if not latest or latest['version']!=provider['version']:raise HTTPException(409,'Workload policy changed during exchange; retry')
            if store.one('SELECT id FROM workload_assertions WHERE id=?',(assertion_id,)):raise HTTPException(401,'Workload assertion was already exchanged')
            store.insert('workload_assertions',{'id':assertion_id,'provider_id':provider['id'],'expires':claims['exp']+5})
            subject='workload:'+provider['id']+':'+claims['sub']
            account=store.one("SELECT id FROM users WHERE issuer='workload' AND subject=?",(subject,))
            if not account:
                account={'id':uid('usr_')}
                store.insert('users',{**account,'email':'','name':claims['sub'][:100],'role':'viewer','issuer':'workload','subject':subject,'active':1,'created':now()})
            record_id=uid('key_')
            store.insert('api_keys',{'id':record_id,'name':provider['name']+' · workload','token_hash':digest(token_value),'prefix':token_value[:12],
                'project_id':config['project_id'],'team_id':config['team_id'],'gateway_id':config['gateway_id'],'allowed_ports':encode(config['allowed_ports']),
                'created_by':account['id'],'workload_provider_id':provider['id'],'rpm':config['rpm'],'expires':expiry,'created':now()})
            store.audit(account['id'],'workload.exchanged',provider['id'],{'key_id':record_id,'expires':expiry})
        return {'access_token':token_value,'token_type':'Bearer','expires_in':max(0,int(expiry-now())),'key_id':record_id}
