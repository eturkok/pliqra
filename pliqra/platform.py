from .protocol import PROTOCOL_VERSION
"""Platform status and optional idempotent export to a separate analytics database."""
import asyncio
from typing import Annotated

import psycopg
from fastapi import Depends,HTTPException
from pydantic import BaseModel,ConfigDict,Field

from .db import SCHEMA_VERSION,encode,now,uid
from .cluster import NODE_TIMEOUT
from .secrets import SecretResolver

COLUMNS=['id','started','finished','actor_user_id','project_id','team_id','key_id','port_name','model','status','input_tokens','output_tokens','cost_micros','usage_source','latency_ms','delivery','request_kind','updated','ingress_site','model_site']


class AnalyticsInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    dsn:Annotated[str|None,Field(min_length=1,max_length=4096)]=None
    enabled:bool=True
    interval_seconds:Annotated[int,Field(ge=60,le=86400)]=60


async def export_analytics(app):
    store=app.state.store;gateway=app.state.gateway
    with store.transaction():
        target=store.one("SELECT * FROM analytics_targets WHERE id='main' AND enabled=1")
        if not target:raise HTTPException(409,'Configure an enabled analytics target first')
        if target['claim_until'] and target['claim_until']>now():raise HTTPException(409,'Analytics export is already running')
        store.update('analytics_targets','main',{'claim_node':gateway.cluster.node,'claim_until':now()+90})
        workspace=store.setting('workspace_id')
        if not workspace:
            workspace=uid('workspace_');store.set_setting('workspace_id',workspace)
    try:
        with gateway.checking():
            dsn=await SecretResolver(store,app.state.vault,app.state.client).resolve(app.state.vault.decrypt(target['secret']))
            options=psycopg.conninfo.conninfo_to_dict(dsn)
            if options.get('host','localhost') not in ('localhost','127.0.0.1','::1') and options.get('sslmode') not in ('verify-ca','verify-full'):
                raise ValueError('Remote analytics databases require verified TLS')
            rows=store.all('SELECT '+','.join(COLUMNS)+' FROM events WHERE updated>? OR (updated=? AND id>?) ORDER BY updated,id LIMIT 500',(target['cursor_time'],target['cursor_time'],target['cursor_id']))
            async with asyncio.timeout(30):
                async with await psycopg.AsyncConnection.connect(dsn,connect_timeout=5) as connection:
                    await connection.execute("SET LOCAL statement_timeout TO '10s'")
                    await connection.execute('CREATE TABLE IF NOT EXISTS pliqra_events (workspace_id TEXT NOT NULL,id TEXT NOT NULL,source_updated DOUBLE PRECISION NOT NULL,started DOUBLE PRECISION,project_id TEXT,team_id TEXT,user_id TEXT,status TEXT,model TEXT,input_tokens BIGINT,output_tokens BIGINT,cost_micros BIGINT,document TEXT NOT NULL,PRIMARY KEY(workspace_id,id))')
                    records=[(workspace,r['id'],r['updated'],r['started'],r['project_id'],r['team_id'],r['actor_user_id'],r['status'],r['model'],r['input_tokens'],r['output_tokens'],r['cost_micros'],encode(r)) for r in rows]
                    if records:
                        async with connection.cursor() as cursor:
                            await cursor.executemany('INSERT INTO pliqra_events VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(workspace_id,id) DO UPDATE SET source_updated=excluded.source_updated,started=excluded.started,project_id=excluded.project_id,team_id=excluded.team_id,user_id=excluded.user_id,status=excluded.status,model=excluded.model,input_tokens=excluded.input_tokens,output_tokens=excluded.output_tokens,cost_micros=excluded.cost_micros,document=excluded.document WHERE pliqra_events.source_updated<=excluded.source_updated',records)
            with store.transaction():
                current=store.one("SELECT claim_node FROM analytics_targets WHERE id='main'")
                if current['claim_node']!=gateway.cluster.node:raise HTTPException(409,'Analytics export lease changed')
                fields={'last_success':now(),'last_error':None,'next_export':now()+(1 if len(rows)==500 else target['interval_seconds'])}
                if rows:fields.update(cursor_time=rows[-1]['updated'],cursor_id=rows[-1]['id'])
                store.update('analytics_targets','main',fields)
            return {'exported':len(rows),'raw_content_included':False,'workspace_id':workspace}
    except asyncio.CancelledError:raise
    except Exception:
        store.update('analytics_targets','main',{'last_error':'Analytics export failed; check target availability, credentials, permissions and TLS','next_export':now()+60})
        raise HTTPException(502,'Analytics export failed; workspace inference is unaffected')
    finally:
        store.run("UPDATE analytics_targets SET claim_node=NULL,claim_until=NULL WHERE id='main' AND claim_node=?",(gateway.cluster.node,))


async def analytics_worker(app):
    while True:
        await asyncio.sleep(5)
        if app.state.gateway.maintenance:continue
        target=app.state.store.one("SELECT next_export FROM analytics_targets WHERE id='main' AND enabled=1")
        if target and target['next_export']<=now():
            try:await export_analytics(app)
            except HTTPException:pass


def mount(app):
    store=app.state.store;admin=app.state.context['admin']

    @app.get('/api/v1/platform/status',tags=['Platform'])
    def status(p=Depends(admin)):
        return {'storage':store.backend,'storage_label':'Shared PostgreSQL' if store.backend=='postgresql' else 'Local SQLite',
            'live_replicas':store.one('SELECT COUNT(*) AS n FROM runtime_nodes WHERE heartbeat>?',(store.clock()-NODE_TIMEOUT,))['n'],
            'active_requests':store.one("SELECT COUNT(*) AS n FROM runtime_leases WHERE kind!='management'")['n'],
            'current_node':app.state.gateway.cluster.node,'topology':app.state.gateway.cluster.topology.model_dump(),'quota_consistency':'single-writable-authority','schema':SCHEMA_VERSION,'organization':app.state.context.get('organization'),'protocol':PROTOCOL_VERSION,
            'interfaces':['chat','embeddings','speech','transcription','responses','anthropic','ollama'],
            'replicas':store.all('SELECT id,heartbeat,site,region FROM runtime_nodes ORDER BY heartbeat DESC')}

    @app.get('/api/v1/analytics',tags=['Platform'])
    def analytics(p=Depends(admin)):
        row=store.one("SELECT * FROM analytics_targets WHERE id='main'")
        return {k:v for k,v in row.items() if k!='secret'}|{'configured':True,'has_credentials':bool(row['secret'])} if row else {'configured':False}

    @app.put('/api/v1/analytics',tags=['Platform'])
    def save(body:AnalyticsInput,p=Depends(admin)):
        previous=store.one("SELECT * FROM analytics_targets WHERE id='main'")
        if not previous and not body.dsn:raise HTTPException(422,'Provide a PostgreSQL analytics connection string or secret reference')
        if body.dsn and not body.dsn.startswith('secret://'):
            try:psycopg.conninfo.conninfo_to_dict(body.dsn)
            except psycopg.Error:raise HTTPException(422,'Invalid analytics connection string')
        with store.transaction():
            if previous and previous['claim_until'] and previous['claim_until']>now():raise HTTPException(409,'Wait for the current export before changing its target')
            row={'id':'main','secret':app.state.vault.encrypt(body.dsn) if body.dsn else previous['secret'],'enabled':int(body.enabled),'interval_seconds':body.interval_seconds}
            if previous:
                if body.dsn:row.update(cursor_time=0,cursor_id='',next_export=0)
                store.update('analytics_targets','main',{k:v for k,v in row.items() if k!='id'})
            else:store.insert('analytics_targets',row)
            store.audit(p['id'],'analytics.configured','main',{'enabled':body.enabled})
        return {'configured':True,'enabled':body.enabled}

    @app.post('/api/v1/analytics/export',tags=['Platform'])
    async def export(p=Depends(admin)):
        result=await export_analytics(app);store.audit(p['id'],'analytics.exported','main',result);return result
