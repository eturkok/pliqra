"""Independent organization workspaces with separate keys, sessions and storage."""
import asyncio
import base64
import hashlib
import hmac
import secrets
from typing import Annotated

import httpx
from fastapi import Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .db import now, uid


class OrganizationInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name:Annotated[str,Field(min_length=1,max_length=100)]
    slug:Annotated[str,Field(pattern=r'^[a-z][a-z0-9-]{1,40}$')]
    owner_email:Annotated[str,Field(min_length=3,max_length=254)]
    owner_name:Annotated[str,Field(min_length=1,max_length=100)]='Organization owner'


class OrganizationState(BaseModel):
    enabled:bool


class Workspaces:
    def __init__(self,app):
        self.app=app;self.store=app.state.store;self.apps={};self.lifespans={};self.lock=asyncio.Lock()

    async def open(self,row):
        async with self.lock:
            if row['id'] in self.apps:return self.apps[row['id']]
            from .main import create_app
            master=self.app.state.vault.key
            key=base64.urlsafe_b64encode(hmac.new(master,('pliqra-org:'+row['id']).encode(),hashlib.sha256).digest())
            schema='org_'+hashlib.sha256((getattr(self.store,'schema','')+row['id']).encode()).hexdigest()[:40]
            public=self.app.state.context['public_url']
            child=create_app(self.store.directory/'organizations'/row['id'],telemetry_runtime=self.app.state.telemetry,database_url=getattr(self.store,'database_url',''),database_schema=schema,encryption_key=key,organization=row,runtime_topology=self.store.runtime_topology,public_url_override=public+'/org/'+row['slug'] if public else '')
            lifespan=child.router.lifespan_context(child)
            try:await lifespan.__aenter__()
            except BaseException:
                child.state.store.close()
                raise
            self.apps[row['id']]=child;self.lifespans[row['id']]=lifespan
            return child

    async def close(self):
        for lifespan in list(self.lifespans.values()):await lifespan.__aexit__(None,None,None)
        self.apps.clear();self.lifespans.clear()


class OrganizationRouter:
    def __init__(self,app,workspaces):self.app=app;self.workspaces=workspaces

    async def __call__(self,scope,receive,send):
        if scope['type']=='http' and scope.get('path','').startswith('/org/'):
            pieces=scope['path'].split('/',3)
            slug=pieces[2]
            row=self.workspaces.store.one('SELECT * FROM organizations WHERE slug=?',(slug,))
            if not row or not row['enabled'] or row['state']!='ready':
                await JSONResponse({'detail':'Organization is unavailable'},404)(scope,receive,send)
                return
            child=await self.workspaces.open(row)
            prefix='/org/'+slug
            nested={**scope,'root_path':scope.get('root_path','')+prefix}
            await child(nested,receive,send)
            return
        await self.app(scope,receive,send)


def mount(app):
    manager=Workspaces(app)
    app.state.organizations=manager
    app.add_middleware(OrganizationRouter,workspaces=manager)
    store=app.state.store;admin=app.state.context['admin']

    @app.get('/api/v1/organizations',tags=['Organizations'])
    def organizations(p=Depends(admin)):
        return [{**row,'url':'/org/'+row['slug']+'/'} for row in store.all('SELECT * FROM organizations ORDER BY created')]

    @app.post('/api/v1/organizations',tags=['Organizations'],status_code=201)
    async def create(body:OrganizationInput,p=Depends(admin)):
        with store.transaction():
            if store.one('SELECT COUNT(*) AS n FROM organizations')['n']>=50:raise HTTPException(422,'At most 50 organizations are supported per control plane')
            row={'id':uid('org_'),'name':body.name,'slug':body.slug,'state':'initializing','enabled':1,'created':now()}
            store.insert('organizations',row)
        password=secrets.token_urlsafe(24)
        try:
            child=await manager.open(row)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=child),base_url='http://organization.internal') as client:
                response=await client.post('/api/v1/setup',json={'name':body.owner_name,'email':body.owner_email,'password':password})
            if response.status_code!=200:raise ValueError('Organization initialization failed')
        except Exception:
            store.update('organizations',row['id'],{'state':'error','enabled':0})
            store.audit(p['id'],'organization.initialization_failed',row['id'])
            raise HTTPException(503,'Organization initialization failed; inspect service logs before retrying')
        with store.transaction():
            store.update('organizations',row['id'],{'state':'ready'})
            store.audit(p['id'],'organization.created',row['id'],{'slug':body.slug})
        return {**row,'state':'ready','url':'/org/'+body.slug+'/','owner_email':body.owner_email,'initial_password':password,'credential_notice':'Shown once. Ask the owner to change this password after signing in.'}

    @app.patch('/api/v1/organizations/{record_id}',tags=['Organizations'])
    async def change(record_id:str,body:OrganizationState,p=Depends(admin)):
        row=store.one('SELECT * FROM organizations WHERE id=?',(record_id,))
        if not row:raise HTTPException(404,'Organization not found')
        if row['state']!='ready':raise HTTPException(409,'Organization initialization is incomplete')
        child=await manager.open(row)
        child.state.store.set_setting('organization_enabled',body.enabled)
        with store.transaction():
            store.update('organizations',record_id,{'enabled':int(body.enabled)})
            store.audit(p['id'],'organization.updated',record_id,{'enabled':body.enabled})
        return {'ok':True}
