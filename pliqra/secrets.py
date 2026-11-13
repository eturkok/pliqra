"""Explicit references to environment, mounted secrets and Vault KV v2."""
import json
import os
import re
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit,unquote,quote
from .telemetry import traced

from fastapi import Depends,HTTPException
from pydantic import BaseModel,ConfigDict,Field,model_validator

from .config import env
from .connectors import response_json
from .db import encode,now,uid
from .security import endpoint_url


class SecretProviderInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name:Annotated[str,Field(min_length=1,max_length=100)]
    kind:Literal['env','file','vault']
    keys:dict[str,str]={}
    endpoint:str=''
    mount:Annotated[str,Field(pattern=r'^[A-Za-z0-9_-]{1,100}$')]='secret'
    allowed_prefixes:Annotated[list[str],Field(max_length=100)]=[]
    token:Annotated[str|None,Field(max_length=4096)]=None
    enabled:bool=True

    @model_validator(mode='after')
    def valid(self):
        if len(self.keys)>100:raise ValueError('At most 100 named secrets per provider')
        if self.kind=='vault':
            self.endpoint=endpoint_url(self.endpoint)
            if urlsplit(self.endpoint).scheme!='https' and urlsplit(self.endpoint).hostname not in ('localhost','127.0.0.1','::1'):
                raise ValueError('Vault requires HTTPS')
            if not self.allowed_prefixes:raise ValueError('Allow at least one explicit Vault path prefix')
        for key in self.keys:
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',key):raise ValueError('Invalid secret alias')
        for value in self.keys.values():
            if self.kind=='env' and (not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,100}',value) or value in ('PLIQRA_ENCRYPTION_KEY','MODELPORT_ENCRYPTION_KEY')):
                raise ValueError('Invalid or reserved environment variable')
            if self.kind=='file' and (Path(value).is_absolute() or '..' in Path(value).parts):raise ValueError('Use a relative mounted secret path')
        for prefix in self.allowed_prefixes:
            if not re.fullmatch(r'[A-Za-z0-9_/-]+',prefix) or '..' in prefix.split('/'):raise ValueError('Invalid Vault prefix')
        return self


class SecretTest(BaseModel):
    reference:Annotated[str,Field(min_length=1,max_length=4096)]


class SecretResolver:
    def __init__(self,store,vault,client):self.store=store;self.vault=vault;self.client=client

    @traced("secret.resolve")
    async def resolve(self,value):
        if not value.startswith('secret://'):return value
        try:
            parsed=urlsplit(value);alias=unquote(parsed.path.lstrip('/'));field=unquote(parsed.fragment) or 'value'
            if parsed.query or not re.fullmatch(r'[A-Za-z0-9_/-]{1,500}',alias) or '..' in alias.split('/'):
                raise ValueError()
            provider=self.store.one('SELECT * FROM secret_providers WHERE id=? AND enabled=1',(parsed.netloc,))
            if not provider:raise ValueError()
            config=json.loads(provider['config'])
            if provider['kind']=='env':
                result=os.environ[config['keys'][alias]]
            elif provider['kind']=='file':
                root=Path(env('PLIQRA_SECRET_ROOT','/run/secrets')).resolve()
                target=(root/config['keys'][alias]).resolve()
                if not target.is_relative_to(root) or target.stat().st_size>4096:raise ValueError()
                result=target.read_text().rstrip('\r\n')
            else:
                prefixes=config['allowed_prefixes']
                if not any(alias==p.rstrip('/') or alias.startswith(p.rstrip('/')+'/') for p in prefixes):raise ValueError()
                token=self.vault.decrypt(provider['secret'])
                async with self.client.stream('GET',config['endpoint']+'/v1/'+config['mount']+'/data/'+quote(alias,safe='/'),headers={'X-Vault-Token':token},timeout=5) as response:
                    response.raise_for_status()
                    data=await response_json(response,100000)
                result=data['data']['data'][field]
            if not isinstance(result,str) or not result or len(result)>4096:raise ValueError()
            return result
        except Exception:
            raise HTTPException(503,'Referenced secret is unavailable; check provider access and the configured alias')


def mount(app):
    store=app.state.store;vault=app.state.vault;admin=app.state.context['admin']

    def public(row):return {k:v for k,v in row.items() if k!='secret'}|{'config':json.loads(row['config']),'has_token':bool(row['secret'])}

    @app.get('/api/v1/secret-providers',tags=['Secret providers'])
    def providers(p=Depends(admin)):
        return [public(row) for row in store.all('SELECT * FROM secret_providers ORDER BY created')]

    def save(body,p,record_id=None):
        previous=store.one('SELECT * FROM secret_providers WHERE id=?',(record_id,)) if record_id else None
        if record_id and not previous:raise HTTPException(404,'Secret provider not found')
        if previous and previous['kind']!=body.kind:raise HTTPException(409,'Create a new provider to change its kind')
        record_id=record_id or uid('sec_')
        data={'id':record_id,'name':body.name,'kind':body.kind,'config':encode(body.model_dump(exclude={'name','kind','token','enabled'})),
              'secret':vault.encrypt(body.token) if body.token is not None else previous['secret'] if previous else '',
              'enabled':int(body.enabled),'version':previous['version']+1 if previous else 1,'created':previous['created'] if previous else now()}
        with store.transaction():
            if previous:store.update('secret_providers',record_id,{k:v for k,v in data.items() if k!='id'})
            else:store.insert('secret_providers',data)
            store.audit(p['id'],'secret_provider.saved',record_id,{'kind':body.kind,'version':data['version']})
        return public(data)

    @app.post('/api/v1/secret-providers',tags=['Secret providers'],status_code=201)
    def create(body:SecretProviderInput,p=Depends(admin)):return save(body,p)

    @app.put('/api/v1/secret-providers/{record_id}',tags=['Secret providers'])
    def update(record_id:str,body:SecretProviderInput,p=Depends(admin)):return save(body,p,record_id)

    @app.post('/api/v1/secret-providers/test',tags=['Secret providers'])
    async def test(body:SecretTest,p=Depends(admin)):
        if not body.reference.startswith('secret://'):raise HTTPException(422,'Use a secret reference')
        with app.state.gateway.checking():
            await app.state.gateway.secrets.resolve(body.reference)
            store.audit(p['id'],'secret_reference.tested','secret-provider')
        return {'resolved':True,'value_included':False}
