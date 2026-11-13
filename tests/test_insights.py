import json

import pytest
from tests.test_app import workspace, connect, key, call
from tests.test_fabric import gateway, bound_key, invoke
from tests.test_behavior import observed, schedule, run
from tests.test_product import local_user, sign_in


def test_diagnosis_is_read_only_and_respects_limits(workspace):
    client, app, captured = workspace
    service = connect(client)['services'][0]
    profile = gateway(client, [service['id']], limits=[{'metric':'requests','scope':'gateway','limit':1,'window_seconds':60}])
    token, headers = bound_key(client, profile)
    before = len(captured)
    body = {'service':service['name'],'key_id':token['id'],'stream':False}
    report = client.post('/api/v1/diagnostics', json=body)
    assert report.status_code == 200, report.text
    assert report.json()['ready'] is True
    assert report.json()['model_calls'] == 0
    assert len(captured) == before
    assert app.state.store.one('SELECT COUNT(*) AS n FROM events')['n'] == 0
    assert app.state.store.one('SELECT COUNT(*) AS n FROM gateway_requests')['n'] == 0
    response = invoke(client, service['name'], headers=headers, max_tokens=128)
    assert response.status_code == 200, response.text
    report = client.post('/api/v1/diagnostics', json=body).json()
    assert report['ready'] is False
    assert any(not r['passed'] for c in report['checks'] for r in c.get('limits',[]))
    app.state.store.update('api_keys', token['id'], {'active':0})
    denied = client.post('/api/v1/diagnostics', json=body).json()
    assert denied['checks'][0]['status'] == 401
    assert 'provider-secret-value' not in json.dumps(denied)


def test_optional_discovery_does_not_claim_inference_readiness(workspace):
    client, app, captured = workspace
    connect(client)
    connection = client.get('/api/v1/connections').json()[0]
    before = len(captured)
    body = {'connection_id':connection['id']}
    assert client.post('/api/v1/diagnostics',json=body).json()['ready'] is None
    assert len(captured) == before
    report = client.post('/api/v1/diagnostics',json={**body,'inspect_endpoint':True}).json()
    assert report['ready'] and report['endpoint_checked'] and report['model_calls'] == 0
    assert len(captured) == before + 1 and captured[-1].method == 'GET'
    app.state.store.update('connections',connection['id'],{'endpoint':'https://offline.test/v1'})
    result = client.post('/api/v1/diagnostics',json={**body,'inspect_endpoint':True}).json()
    assert result['checks'][-1]['category'] == 'connectivity'
    assert client.post('/api/v1/diagnostics',json={'connection_id':'missing'}).json()['ready'] is False


def test_diagnosis_stale_contract_and_admin_boundary(workspace):
    client, app, _ = workspace
    service = connect(client)['services'][0]
    binding = client.post('/protocol/v1/bindings',json={'service':service['name']}).json()
    port = client.get('/api/v1/ports').json()[0]
    assert client.patch('/api/v1/ports/'+port['id'],json={**port,'description':'changed'}).status_code == 200
    report = client.post('/api/v1/diagnostics',json={'service':service['name'],'binding_id':binding['id']}).json()
    assert report['ready'] is False and report['checks'][-1]['status'] == 409
    local_user(client, 'Viewer')
    client.post('/api/v1/logout',json={})
    sign_in(client,'viewer@example.test')
    assert client.post('/api/v1/diagnostics',json={'service':service['name']}).status_code == 403
    assert client.get('/api/v1/diagnostics/runtime').status_code == 403
    assert client.get('/api/v1/behavior-checks/missing/impact').status_code == 403


def test_behavior_failure_links_bindings_and_eligible_alternative(observed):
    client, app, deployment, control = observed
    service = client.get('/api/v1/ports').json()[0]
    # The same endpoint can fail behavioral assertions without losing declared tools.
    client.patch('/api/v1/deployments/'+deployment['id'],json={'capabilities':['chat','tools','streaming']})
    second = connect(client,endpoint='https://other.test',model='alternative',provider='openai')
    alt = second['deployments'][0] if 'deployments' in second else client.get('/api/v1/connections').json()[0]['models'][0]
    # Find the newly added deployment independently of ordering.
    alt = next(d for c in client.get('/api/v1/connections').json() for d in c['models'] if d['id'] != deployment['id'])
    client.patch('/api/v1/deployments/'+alt['id'],json={'capabilities':['chat','tools']})
    client.patch('/api/v1/ports/'+service['id'],json={**service,'deployments':[deployment['id'],alt['id']]})
    token = key(client)
    binding = client.post('/protocol/v1/bindings',headers={'Authorization':'Bearer '+token['token']},json={'service':service['name'],'requirements':['tools']})
    assert binding.status_code == 201, binding.text
    check = schedule(client,deployment['id'],checks=[{'name':'Tool','capability':'tools'}])
    control['mode'] = 'changed'
    assert run(client,check['id'])['status'] == 'failed'
    before = control['calls']
    report = client.get('/api/v1/behavior-checks/'+check['id']+'/impact').json()
    assert report['summary']['alternative_available'] == 1, report
    assert report['items'][0]['alternatives'][0]['id'] == alt['id']
    assert control['calls'] == before
    assert report['items'][0]['binding_id'] == binding.json()['id']
    app.state.store.update('api_keys',token['id'],{'active':0})
    assert client.get('/api/v1/behavior-checks/'+check['id']+'/impact').json()['total'] == 0
    app.state.store.update('deployments',deployment['id'],{'metadata_hash':'new-observation'})
    assert client.get('/api/v1/behavior-checks/'+check['id']+'/impact').json()['observation_stale']


def test_disabled_connection_and_disabled_gateway_capability_are_explicit(workspace):
    client,app,captured = workspace
    service = connect(client)['services'][0]
    connection = client.get('/api/v1/connections').json()[0]
    profile = gateway(client,[service['id']],allow_tools=False)
    token,_ = bound_key(client,profile)
    report = client.post('/api/v1/diagnostics',json={'service':service['name'],'key_id':token['id'],'requirements':['tools'],'stream':False}).json()
    assert report['ready'] is False
    app.state.store.update('connections',connection['id'],{'enabled':0})
    before = len(captured)
    report = client.post('/api/v1/diagnostics',json={'connection_id':connection['id'],'inspect_endpoint':True}).json()
    assert report['ready'] is False
    assert len(captured) == before


@pytest.mark.parametrize('scope',['key','user'])
def test_diagnosis_uses_the_same_quota_identity_as_admission(workspace,scope):
    client,app,_ = workspace
    service=connect(client)['services'][0]
    profile=gateway(client,[service['id']],limits=[{'metric':'requests','scope':scope,'limit':1,'window_seconds':3600}])
    first,headers=bound_key(client,profile)
    second,_=bound_key(client,profile)
    assert invoke(client,service['name'],headers=headers,max_tokens=32).status_code == 200
    selected=second if scope=='user' else first
    report=client.post('/api/v1/diagnostics',json={'service':service['name'],'key_id':selected['id'],'stream':False}).json()
    limits=[q for check in report['checks'] for q in check.get('limits',[])]
    assert any(q['name']=='Gateway '+scope+' requests' and q['used']==1 and not q['passed'] for q in limits)
    assert report['ready'] is False


def test_first_behavior_impact_is_untested_not_stale(workspace):
    client,app,captured=workspace
    connect(client)
    deployment=client.get('/api/v1/connections').json()[0]['models'][0]
    check=schedule(client,deployment['id'])
    before=len(captured)
    report=client.get('/api/v1/behavior-checks/'+check['id']+'/impact').json()
    assert report['observation_state']=='not_tested' and report['observation_stale'] is False
    assert report['total']==0 and len(captured)==before


def test_canary_candidate_failure_is_visible_to_stable_service_bindings(observed,monkeypatch):
    from pliqra import quality
    client,app,target,control=observed
    candidate=app.state.gateway.port(client.get('/api/v1/ports').json()[0]['id'])
    stable_service=connect(client,endpoint='https://stable.test',model='stable',provider='openai')['services'][0]
    stable=app.state.gateway.port(stable_service['id'])
    for connection in client.get('/api/v1/connections').json():
        for deployment in connection['models']:
            client.patch('/api/v1/deployments/'+deployment['id'],json={'capabilities':['chat','tools','streaming']})
    check=schedule(client,target['id'],checks=[{'name':'Tool','capability':'tools'}])
    control['mode']='changed';assert run(client,check['id'])['status']=='failed'
    release={'id':'graph-fixture','version':1,'percent':10,'candidate_id':candidate['id'],'config':{
        'stable_fingerprint':app.state.gateway.base_fingerprint(stable),'candidate_fingerprint':app.state.gateway.base_fingerprint(candidate)}}
    # The existing quality suite exercises publication gates; this isolates the active graph traversal.
    monkeypatch.setattr(quality,'active_release',lambda store,service_id:release if service_id==stable['id'] else None)
    token=key(client)
    binding=client.post('/protocol/v1/bindings',headers={'Authorization':'Bearer '+token['token']},json={'service':stable['name'],'requirements':['tools']})
    assert binding.status_code==201,binding.text
    report=client.get('/api/v1/behavior-checks/'+check['id']+'/impact').json()
    assert report['total']==1 and report['items'][0]['service']==stable['name'],report
    assert report['items'][0]['status']=='alternative_available'
