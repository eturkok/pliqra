"""Real PostgreSQL replica tests. CI supplies PLIQRA_TEST_POSTGRES_URL."""
import asyncio
import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from pliqra.main import create_app
from pliqra.db import now
from tests.test_app import connect
from tests.test_fabric import gateway, bound_key, invoke


@pytest.fixture
def replicas(tmp_path,request):
    url=os.getenv('PLIQRA_TEST_POSTGRES_URL')
    if not url:pytest.skip('Real PostgreSQL integration URL is not configured')
    import psycopg
    schema='test_'+uuid.uuid4().hex
    encryption=Fernet.generate_key()
    topology=getattr(request,'param',[None,None])
    first=create_app(tmp_path/'one',database_url=url,database_schema=schema,encryption_key=encryption,runtime_topology=topology[0])
    first.state.test_org_schemas=[]
    with TestClient(first) as a:
        setup=a.post('/api/v1/setup',json={'name':'Scale owner','email':'owner@example.test','password':'long-local-password'})
        assert setup.status_code==200,setup.text
        a.headers['X-Pliqra-CSRF']=setup.json()['csrf']
        second=create_app(tmp_path/'two',database_url=url,database_schema=schema,encryption_key=encryption,runtime_topology=topology[1])
        with TestClient(second) as b:
            login=b.post('/api/v1/login',json={'email':'owner@example.test','password':'long-local-password'})
            assert login.status_code==200,login.text
            b.headers['X-Pliqra-CSRF']=login.json()['csrf']
            clients=[]
            def handler(request):
                if request.url.path.endswith('/models'):return httpx.Response(200,json={'data':[{'id':'scale-model'}]})
                return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':'Synthetic completion'},'finish_reason':'stop'}],'usage':{'prompt_tokens':4,'completion_tokens':2}})
            for app in (first,second):
                mock=httpx.AsyncClient(transport=httpx.MockTransport(handler))
                app.state.client=app.state.gateway.client=mock
                clients.append(mock)
            try:yield a,b,first,second
            finally:
                for mock in clients:asyncio.run(mock.aclose())
    with psycopg.connect(url,autocommit=True) as db:
        for owned in [schema,*first.state.test_org_schemas]:db.execute(f'DROP SCHEMA "{owned}" CASCADE')


def test_postgres_replica_admission_and_reporting(replicas,monkeypatch):
    a,b,first,second=replicas
    service=connect(a)['services'][0]
    profile=gateway(a,[service['id']],limits=[{'metric':'requests','scope':'gateway','limit':1,'window_seconds':60}])
    _,headers=bound_key(a,profile)
    binding=a.post('/protocol/v1/bindings',headers=headers,json={'service':service['name']})
    assert binding.status_code==201,binding.text
    journey=b.get('/api/v1/getting-started')
    assert journey.status_code==200,journey.text
    barrier=threading.Barrier(2)
    for app in (first,second):
        prepare=app.state.gateway.prepare
        def synchronized(*args,_prepare=prepare,**kwargs):
            value=_prepare(*args,**kwargs)
            barrier.wait(timeout=10)
            return value
        monkeypatch.setattr(app.state.gateway,'prepare',synchronized)
    with ThreadPoolExecutor(2) as pool:
        futures=[pool.submit(invoke,client,service['name'],headers) for client in (a,b)]
        responses=[future.result(timeout=20) for future in futures]
    assert sorted(response.status_code for response in responses)==[200,429]
    assert first.state.store.one("SELECT COUNT(*) AS n FROM gateway_requests WHERE status='success'")['n']==1
    report=b.get('/api/v1/usage?days=0')
    assert report.status_code==200,report.text
    assert report.json()['summary']['successful']==1
    assert b.get('/api/v1/operations').status_code==200
    assert b.get('/api/v1/gateways').status_code==200


def test_postgres_concurrency_and_stale_replica_fencing(replicas):
    a,b,first,second=replicas
    store=first.state.store
    store.set_setting('runtime',{'max_concurrency':1,'health_checks':False})
    lease=first.state.gateway.cluster.acquire('test')
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as full:second.state.gateway.cluster.acquire('test')
    assert full.value.status_code==429
    second.state.store.run('UPDATE runtime_nodes SET heartbeat=? WHERE id=?',(now()-120,first.state.gateway.cluster.node))
    with second.state.store.transaction():second.state.gateway.cluster.reap()
    recovered=second.state.gateway.cluster.acquire('test')
    with pytest.raises(HTTPException) as stale:first.state.gateway.cluster.acquire('test')
    assert stale.value.status_code==503
    second.state.gateway.cluster.release(recovered)
    assert not store.one('SELECT id FROM runtime_leases WHERE id=?',(lease,))


@pytest.mark.parametrize('stream', [False, True])
def test_postgres_attempt_and_quota_settlement_roll_back_together(replicas, monkeypatch, stream):
    a, b, first, second = replicas
    service = connect(a)['services'][0]
    from tests.test_app import price, Fragments
    price(a)
    profile = gateway(a, [service['id']])
    _, headers = bound_key(a, profile)

    def fail(*args):
        raise RuntimeError('Injected settlement failure')

    mock = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200,
        headers={'content-type': 'text/event-stream'}, stream=Fragments(
            'data: {"choices":[{"delta":{"content":"Synthetic"}}]}\n\n'
            'data: {"choices":[],"usage":{"prompt_tokens":4,"completion_tokens":2}}\n\n'
            'data: [DONE]\n\n')))) if stream else None
    if mock: first.state.gateway.client = mock
    monkeypatch.setattr(first.state.gateway.policies, 'settle', fail)
    try:
        with pytest.raises(RuntimeError, match='Injected settlement failure'):
            invoke(a, service['name'], headers, stream=stream)
        # The second replica must see no partial event success or freed quota.
        for table in ('events', 'gateway_requests'):
            assert second.state.store.one(f'SELECT status FROM {table}')['status'] == 'pending'
        assert second.state.store.one('SELECT outcome FROM attempts')['outcome'] == ('pending' if stream else 'success')
    finally:
        if mock: asyncio.run(mock.aclose())


def test_postgres_encrypted_logical_backup_rolls_back_invalid_data(replicas):
    a,b,first,second=replicas
    from pliqra.operations import backup_bytes,restore_bytes
    from pliqra.logical_archive import archive_key
    from fastapi import HTTPException
    store,vault=first.state.store,first.state.vault
    connect(a)
    archive=backup_bytes(store,vault,'long-backup-password')
    assert archive.startswith(b'PLQLOGI01')
    salt=archive[9:25]; cipher=Fernet(archive_key('long-backup-password',salt))
    payload=json.loads(cipher.decrypt(archive[25:]))
    original=store.one('SELECT COUNT(*) AS n FROM users')['n']
    payload['tables']['sessions'].append({'token_hash':'invalid','user_id':'missing-user','csrf':'synthetic','expires':now()+60,'federation_context':'{}'})
    invalid=archive[:25]+cipher.encrypt(json.dumps(payload).encode())
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):restore_bytes(store,vault,invalid,'long-backup-password')
    assert store.one('SELECT COUNT(*) AS n FROM users')['n']==original
    assert store.one('SELECT COUNT(*) AS n FROM connections')['n']==1
    result=restore_bytes(store,vault,archive,'long-backup-password')
    assert result['restored']
    store.set_setting('maintenance',False)
    assert b.get('/api/v1/session').json()['user'] is None


def test_postgres_reconnect_and_fresh_replica_do_not_replay_or_interrupt_work(replicas,tmp_path):
    a,b,first,second=replicas
    from pliqra.db import Store
    store=first.state.store
    store.insert('events',{'id':'pending-is-not-replayed','started':now(),'principal':'synthetic','status':'pending','stream':0,'node_id':first.state.gateway.cluster.node})
    another=Store(tmp_path/'third',store.database_url,store.schema)
    assert another.one("SELECT status FROM events WHERE id='pending-is-not-replayed'")['status']=='pending'
    another.close()
    store.set_setting('reconnect-proof',{'value':'persisted'})
    store.conn.close()
    assert store.setting('reconnect-proof')=={'value':'persisted'}
    store.update('events','pending-is-not-replayed',{'status':'interrupted'})


def test_analytics_upserts_reconciled_usage_without_raw_content(replicas):
    a,b,first,second=replicas
    import psycopg
    from psycopg.conninfo import make_conninfo
    from tests.test_app import call
    schema='analytics_'+uuid.uuid4().hex
    with psycopg.connect(first.state.store.database_url,autocommit=True) as db:db.execute(f'CREATE SCHEMA "{schema}"')
    dsn=make_conninfo(first.state.store.database_url,options='-c search_path='+schema)
    try:
        service=connect(a)['services'][0]
        response=call(a,service['name']);assert response.status_code==200,response.text
        event=response.headers['X-Request-ID']
        configured=a.put('/api/v1/analytics',json={'dsn':dsn,'enabled':True})
        assert configured.status_code==200,configured.text
        exported=a.post('/api/v1/analytics/export',json={})
        assert exported.status_code==200,exported.text
        assert exported.json()['exported']==1
        assert 'pliqra-test-only' not in a.get('/api/v1/analytics').text
        fixed=a.post('/api/v1/events/'+event+'/reconcile',json={'cost_micros':7,'input_tokens':40,'output_tokens':3,'note':'Synthetic provider verification'})
        assert fixed.status_code==200,fixed.text
        assert a.post('/api/v1/analytics/export',json={}).status_code==200
        with psycopg.connect(dsn) as db:
            rows=db.execute('SELECT cost_micros,document FROM pliqra_events').fetchall()
        assert len(rows)==1 and rows[0][0]==7
        assert 'Private prompt' not in rows[0][1] and 'provider-secret-value' not in rows[0][1]
        assert a.put('/api/v1/analytics',json={'enabled':False}).status_code==200
    finally:
        with psycopg.connect(first.state.store.database_url,autocommit=True) as db:db.execute(f'DROP SCHEMA "{schema}" CASCADE')


def test_postgres_workflows_and_organization_visibility_across_replicas(replicas):
    from tests.test_workflows import wait_for
    a,b,first,second=replicas
    stable=connect(a)['services'][0];candidate=connect(a)['services'][0]
    prompt=a.post('/api/v1/prompts',json={'name':'Shared instructions','messages':[{'role':'user','content':'Case {{value}}'}]})
    assert prompt.status_code==201,prompt.text
    assert b.get('/api/v1/prompts').json()[0]['id']==prompt.json()['id']
    batch=a.post('/api/v1/batches',json={'name':'Shared durable work','items':[{'body':{'model':stable['name'],'prompt':{'id':prompt.json()['id'],'version':1,'variables':{'value':'synthetic'}}}}]})
    assert batch.status_code==202,batch.text
    finished=wait_for(b,'/api/v1/batches/'+batch.json()['id'])
    assert finished['status']=='completed',finished
    dataset=a.post('/api/v1/evaluation-sets',json={'name':'Shared release cases','cases':[{'name':str(i),'prompt':'Synthetic','contains':'Synthetic'} for i in range(3)]}).json()
    evaluated=a.post('/api/v1/evaluations',json={'name':'Shared comparison','dataset_id':dataset['id'],'services':[stable['name'],candidate['name']]})
    assert evaluated.status_code==202,evaluated.text
    evidence=wait_for(b,'/api/v1/evaluations/'+evaluated.json()['id'])
    release=b.post('/api/v1/releases',json={'service':stable['name'],'candidate':candidate['name'],'evaluation_id':evidence['id'],'percent':20})
    assert release.status_code==201,release.text
    assert a.get('/api/v1/releases').json()[0]['id']==release.json()['id']
    org=a.post('/api/v1/organizations',json={'name':'Shared isolated organization','slug':'shared-lab','owner_email':'org@example.test','owner_name':'Org owner'})
    assert org.status_code==201,org.text
    created=org.json()
    login=b.post('/org/shared-lab/api/v1/login',json={'email':'org@example.test','password':created['initial_password']})
    assert login.status_code==200,login.text
    assert b.get('/org/shared-lab/api/v1/prompts').json()==[]
    assert first.state.store.one('SELECT COUNT(*) AS n FROM prompts')['n']==1
    # Organization schemas are independent of the disposable root fixture schema.
    import hashlib
    schema='org_'+hashlib.sha256((first.state.store.schema+created['id']).encode()).hexdigest()[:40]
    first.state.test_org_schemas.append(schema)


def test_postgres_lost_connection_fences_only_affected_replica(replicas):
    import psycopg
    a,b,first,second=replicas
    pid=first.state.store.one('SELECT pg_backend_pid() AS pid')['pid']
    with psycopg.connect(first.state.store.database_url,autocommit=True) as db:db.execute('SELECT pg_terminate_backend(%s)',(pid,))
    failed=a.get('/api/v1/session')
    assert failed.status_code==503,failed.text
    assert first.state.gateway.cluster.fenced
    assert a.get('/healthz').status_code==503
    assert b.get('/healthz').status_code==200
    assert b.get('/api/v1/session').json()['user']['role']=='admin'


def test_postgres_publication_impact_and_existing_client_binding_across_replicas(replicas):
    a, b, first, second = replicas
    created = connect(a)['services'][0]
    service = next(item for item in a.get('/api/v1/ports').json() if item['id'] == created['id'])
    profile = gateway(a, [service['id']])
    _, headers = bound_key(a, profile)
    binding = a.post('/protocol/v1/bindings', headers=headers, json={'service': service['name']}).json()
    protected = headers | {'X-Pliqra-Binding': binding['id']}
    assert invoke(b, service['name'], protected).status_code == 200
    proposed = {'name': service['name'], 'deployments': service['deployments'], 'requirements': ['tools']}
    report = b.post('/api/v1/ports/' + service['id'] + '/preview', json=proposed)
    assert report.status_code == 200, report.text
    assert report.json()['impact']['summary']['blocked'] == 1
    expected = {'If-Match': '"' + report.json()['current_fingerprint'] + '"'}
    proposed['requirements'] = ['chat']
    assert a.patch('/api/v1/ports/' + service['id'], headers=expected, json=proposed).status_code == 200
    assert b.patch('/api/v1/ports/' + service['id'], headers=expected, json=proposed).status_code == 412
    assert invoke(b, service['name'], protected).status_code == 409
    renewed = a.post('/protocol/v1/bindings', headers=headers, json={'service': service['name']}).json()
    assert invoke(b, service['name'], headers | {'X-Pliqra-Binding': renewed['id']}).status_code == 200
