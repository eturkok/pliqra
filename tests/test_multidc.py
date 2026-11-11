"""Two site-aware gateways sharing one real PostgreSQL authority; model responses are synthetic."""
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from pliqra.topology import RuntimeTopology
from tests.test_scale import replicas
from tests.test_app import connect,call
from tests.test_fabric import gateway,bound_key,invoke,rail
from tests.test_architecture import swap_upstream

pytestmark=pytest.mark.parametrize('replicas',[(RuntimeTopology(site='dc-west',region='eu',multi_dc=True),RuntimeTopology(site='dc-east',region='eu',multi_dc=True))],indirect=True)


def service_pair(a):
    west=connect(a,endpoint='https://west.test/v1',site='dc-west')
    east=connect(a,endpoint='https://east.test/v1',site='dc-east')
    connections=a.get('/api/v1/connections').json()
    ids=[next(c['models'][0]['id'] for c in connections if c['id']==item['id']) for item in (west,east)]
    created=a.post('/api/v1/ports',json={'name':'site-aware','deployments':ids,'requirements':['chat'],'placement':{'routing':'local_first'}})
    assert created.status_code==201,created.text
    return next(p for p in a.get('/api/v1/ports').json() if p['id']==created.json()['id']),west,east


def test_behavior_checks_respect_endpoint_site_and_site_drain(replicas):
    from fastapi import HTTPException
    from pliqra.behavior import claim, execute
    from tests.test_behavior import schedule
    a,b,first,second=replicas
    connection=connect(a,endpoint='https://west.test/v1',site='dc-west')
    deployment=next(c for c in a.get('/api/v1/connections').json() if c['id']==connection['id'])['models'][0]
    row=schedule(a,deployment['id'],enabled=True,checks=[{'name':'Chat','capability':'chat'}])
    first.state.store.update('behavior_schedules',row['id'],{'next_run':first.state.store.clock()-1})
    assert claim(second) is None
    assert b.post('/api/v1/behavior-checks/'+row['id']+'/run',json={}).status_code==409
    run=claim(first)
    asyncio.run_coroutine_threadsafe(execute(first,run),first.state.loop).result(5)
    assert b.get('/api/v1/behavior-checks').json()['items'][0]['last_status']=='passed'
    event=first.state.store.one("SELECT ingress_site,model_site FROM events WHERE request_kind='probe'")
    assert event=={'ingress_site':'dc-west','model_site':'dc-west'}
    assert b.put('/api/v1/sites/dc-west/mode',json={'mode':'draining','version':1}).status_code==200
    first.state.store.update('behavior_schedules',row['id'],{'next_run':first.state.store.clock()-1})
    with pytest.raises(HTTPException) as blocked:
        claim(first)
    assert blocked.value.status_code==503 and claim(second) is None


def test_multidc_local_preference_and_exact_global_quota(replicas,monkeypatch):
    a,b,first,second=replicas
    service,_,_=service_pair(a)
    for client,site in ((a,'dc-west'),(b,'dc-east')):
        response=call(client,service['name']);assert response.status_code==200,response.text
        assert response.headers['X-Pliqra-Site']==site and response.headers['X-Pliqra-Model-Site']==site
        event=client.get('/api/v1/events/'+response.headers['X-Request-ID']).json()
        assert event['ingress_site']==event['model_site']==site
    profile=gateway(a,[service['id']],limits=[{'metric':'requests','scope':'gateway','limit':1,'window_seconds':60}])
    _,headers=bound_key(a,profile)
    barrier=threading.Barrier(2)
    for app in (first,second):
        prepare=app.state.gateway.prepare
        def synchronize(*args,_prepare=prepare,**kwargs):
            result=_prepare(*args,**kwargs);barrier.wait(timeout=10);return result
        monkeypatch.setattr(app.state.gateway,'prepare',synchronize)
    with ThreadPoolExecutor(2) as pool:
        replies=[pool.submit(invoke,client,service['name'],headers) for client in (a,b)]
        assert sorted(f.result(timeout=20).status_code for f in replies)==[200,429]
    totals=b.get('/api/v1/usage?days=0&ingress_site=dc-east').json()['summary']
    assert totals['successful']>=1


@pytest.mark.parametrize('limit,expected',[('tokens',429),('budget',402)])
def test_multidc_checked_input_reservations_are_atomic(replicas,limit,expected):
    a,b,first,second=replicas
    service=connect(a)['services'][0]
    deployment=a.get('/api/v1/connections').json()[0]['models'][0]
    assert a.patch('/api/v1/deployments/'+deployment['id'],json={'capabilities':['chat','streaming'],'input_price':2,'output_price':3}).status_code==200
    if limit=='budget':assert a.patch('/api/v1/projects/default',json={'name':'Shared budget','monthly_budget':0.15}).status_code==200
    guard=rail(a,config={'rule':'keywords','terms':['x'],'action':'redact'})
    profile=gateway(a,[service['id']],guardrails=[{'guardrail_id':guard['id'],'stages':['input']}],
                    limits=[{'metric':'tokens','limit':75000}] if limit=='tokens' else [])
    _,headers=bound_key(a,profile)
    mocks=[swap_upstream(app,lambda _:httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':'unknown usage'}}]})) for app in (first,second)]
    try:
        with ThreadPoolExecutor(2) as pool:
            futures=[pool.submit(invoke,client,service['name'],headers,content='x'*5000,max_tokens=16) for client in (a,b)]
            assert sorted(f.result(timeout=20).status_code for f in futures)==[200,expected]
        assert first.state.store.one('SELECT COUNT(*) AS n FROM attempts')['n']==1
        assert second.state.store.one('SELECT SUM(tokens) AS n FROM gateway_requests')['n']<75000
    finally:
        for mock in mocks:asyncio.run(mock.aclose())


def test_multidc_draining_preserves_inflight_call_and_redirects_new_admission(replicas):
    a,b,first,second=replicas;service,_,_=service_pair(a)
    entered=threading.Event();release=threading.Event()
    async def upstream(request):
        entered.set();await asyncio.to_thread(release.wait,10)
        return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':'Synthetic in-flight result'}}],'usage':{'prompt_tokens':4,'completion_tokens':2}})
    mock=swap_upstream(first,upstream)
    try:
        with ThreadPoolExecutor(1) as pool:
            running=pool.submit(call,a,service['name'])
            assert entered.wait(5)
            changed=b.put('/api/v1/sites/dc-west/mode',json={'mode':'draining','version':1})
            assert changed.status_code==200,changed.text
            assert a.get('/livez').status_code==200 and a.get('/readyz').status_code==503
            assert call(a,service['name']).status_code==503
            remote=call(b,service['name']);assert remote.status_code==200 and remote.headers['X-Pliqra-Model-Site']=='dc-east'
            release.set();completed=running.result(timeout=10)
            assert completed.status_code==200 and completed.headers['X-Pliqra-Model-Site']=='dc-west'
    finally:release.set();asyncio.run(mock.aclose())
    assert first.state.store.one("SELECT COUNT(*) AS n FROM attempts WHERE outcome='success'")['n']==2


def test_multidc_health_failure_is_local_to_observing_site(replicas):
    a,b,first,second=replicas;_,west,_=service_pair(a)
    def broken(request):return httpx.Response(503)
    mock=swap_upstream(first,broken)
    try:
        checked=a.post('/api/v1/connections/'+west['id']+'/check',json={})
        assert checked.status_code==502,checked.text
        a_view=next(c for c in a.get('/api/v1/connections').json() if c['id']==west['id'])
        assert a_view['status']=='unreachable'
        good=b.post('/api/v1/connections/'+west['id']+'/check',json={})
        assert good.status_code==200,good.text
        assert call(a,west['services'][0]['name']).status_code==422
        assert call(b,west['services'][0]['name']).status_code==200
        assert first.state.store.one('SELECT available FROM deployments WHERE connection_id=?',(west['id'],))['available']==1
    finally:asyncio.run(mock.aclose())


def test_multidc_circuit_breaker_does_not_poison_other_sites(replicas):
    a,b,first,second=replicas;_,west,_=service_pair(a)
    mock=swap_upstream(first,lambda _:httpx.Response(503))
    try:
        for _ in range(3):assert call(a,west['services'][0]['name']).status_code==502
        assert call(a,west['services'][0]['name']).status_code==422
        assert call(b,west['services'][0]['name']).status_code==200
        assert first.state.store.one('SELECT open_until FROM deployments WHERE connection_id=?',(west['id'],))['open_until']==0
    finally:asyncio.run(mock.aclose())


def test_multidc_placement_restrictions_and_database_clock(replicas,monkeypatch):
    a,b,first,second=replicas;service,_,_=service_pair(a)
    port=first.state.gateway.port(service['id'])
    assert a.patch('/api/v1/ports/'+service['id'],json={**port,'placement':{'routing':'local_only','allowed_sites':['dc-west']}}).status_code==200
    assert call(a,service['name']).status_code==200
    assert call(b,service['name']).status_code==422
    from pliqra import gateway as runtime,policies
    monkeypatch.setattr(runtime,'now',lambda:1.0);monkeypatch.setattr(policies,'now',lambda:1.0)
    response=call(a,service['name']);assert response.status_code==200,response.text
    event=first.state.store.one('SELECT started FROM events WHERE id=?',(response.headers['X-Request-ID'],))
    assert abs(event['started']-first.state.store.clock())<5


def test_multidc_partition_fences_only_disconnected_site(replicas):
    import psycopg
    a,b,first,second=replicas;service,_,_=service_pair(a)
    pid=first.state.store.one('SELECT pg_backend_pid() AS id')['id']
    with psycopg.connect(first.state.store.database_url,autocommit=True) as db:db.execute('SELECT pg_terminate_backend(%s)',(pid,))
    assert a.get('/readyz').status_code==503
    assert a.get('/livez').status_code==200
    assert first.state.gateway.cluster.fenced
    assert call(a,service['name']).status_code==503
    assert call(b,service['name']).status_code==200


@pytest.mark.parametrize('limit_kind,scope',[('tokens',scope) for scope in ('gateway','key','user','project','team','service')]+[('concurrency','gateway')])
def test_multidc_pending_reservation_and_concurrency_are_shared(replicas,limit_kind,scope):
    a,b,first,second=replicas;service,_,_=service_pair(a)
    profile=gateway(a,[service['id']],limits=[{'metric':'tokens','scope':scope,'limit':4000,'window_seconds':60}] if limit_kind=='tokens' else [])
    _,headers=bound_key(a,profile)
    if limit_kind=='concurrency':first.state.store.set_setting('runtime',{'max_concurrency':1,'health_checks':False})
    entered=threading.Event();release=threading.Event()
    async def upstream(request):
        entered.set();await asyncio.to_thread(release.wait,10)
        return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':'Synthetic'}}],'usage':{'prompt_tokens':4,'completion_tokens':2}})
    mock=swap_upstream(first,upstream)
    body={'model':service['name'],'messages':[{'role':'user','content':'Synthetic'}],'max_tokens':32}
    try:
        with ThreadPoolExecutor(1) as pool:
            pending=pool.submit(a.post,'/v1/chat/completions',headers=headers,json=body)
            assert entered.wait(5)
            rejected=b.post('/v1/chat/completions',headers=headers,json=body)
            assert rejected.status_code==429,rejected.text
            release.set();assert pending.result(timeout=10).status_code==200
        assert b.post('/v1/chat/completions',headers=headers,json=body).status_code==200
    finally:release.set();asyncio.run(mock.aclose())


def test_multidc_multihost_connection_selects_reachable_writable_authority(replicas,tmp_path):
    import psycopg
    from pliqra.db import Store
    a,b,first,second=replicas
    options=psycopg.conninfo.conninfo_to_dict(first.state.store.database_url)
    host=options.get('host','127.0.0.1');port=options.get('port','5432')
    dsn=psycopg.conninfo.make_conninfo(first.state.store.database_url,host=host+','+host,port='1,'+port,connect_timeout=2)
    store=Store(tmp_path/'multihost',dsn,first.state.store.schema)
    try:
        assert store.one('SELECT COUNT(*) AS n FROM users')['n']==1
        assert 'read-write' in store.database_url
    finally:store.close()


def test_multidc_canary_cannot_expand_stable_model_location_boundary(replicas):
    from pliqra.db import encode,now
    a,b,first,second=replicas;_,west,east=service_pair(a)
    stable=first.state.gateway.port(west['services'][0]['id']);candidate=first.state.gateway.port(east['services'][0]['id'])
    assert a.patch('/api/v1/ports/'+stable['id'],json={**stable,'placement':{'allowed_sites':['dc-west']}}).status_code==200
    stable=first.state.gateway.port(stable['id'])
    # Seed the already-authorized runtime release to isolate routing enforcement.
    first.state.store.insert('releases',{'id':'location-boundary-canary','service_id':stable['id'],'candidate_id':candidate['id'],'evaluation_id':'synthetic-routing-fixture','config':encode({'stable_fingerprint':first.state.gateway.base_fingerprint(stable),'candidate_fingerprint':first.state.gateway.base_fingerprint(candidate)}),'state':'canary','percent':100,'version':1,'actor':'synthetic','created':now()})
    response=call(a,stable['name']);assert response.status_code==200,response.text
    assert response.headers['X-Pliqra-Model-Site']=='dc-west'
    details=a.get('/api/v1/events/'+response.headers['X-Request-ID']).json()
    assert any('placement boundary' in d.get('reason','') for d in details['decisions'])


def test_multidc_manual_endpoint_can_recover_without_model_discovery(replicas):
    a,b,first,second=replicas
    connection=connect(a,endpoint='https://manual.test/v1',provider='openai',model='manual-scale-model',site='dc-west')
    broken=swap_upstream(first,lambda _:httpx.Response(503))
    try:
        assert a.post('/api/v1/connections/'+connection['id']+'/check',json={}).status_code==502
        assert call(a,connection['services'][0]['name']).status_code==422
        def recovered(request):
            return httpx.Response(200,json={} if request.method=='GET' else {'choices':[{'message':{'role':'assistant','content':'Synthetic recovered manual endpoint'}}]})
        healthy=swap_upstream(first,recovered)
        try:
            checked=a.post('/api/v1/connections/'+connection['id']+'/check',json={})
            assert checked.status_code==200 and checked.json()['inference_verified'] is False
            assert call(a,connection['services'][0]['name']).status_code==200
        finally:asyncio.run(healthy.aclose())
    finally:asyncio.run(broken.aclose())
