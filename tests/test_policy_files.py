import json

import pytest
from tests.test_app import workspace, connect
from tests.test_fabric import gateway, rail
from tests.test_product import local_user, sign_in


def manifest(service, *slugs):
    return {'format':'pliqra/policies/1','gateways':[{'name':s.title(),'slug':s,'config':{'services':['chat']}} for s in slugs or ('git-chat',)],'mapping':{'services':{'chat':service}},'source_revision':'a'*40}


def plan(client, document):
    response = client.post('/api/v1/policies/plan',json=document)
    assert response.status_code == 200, response.text
    return response.json()


def apply(client, report):
    return client.post('/api/v1/policies/apply',json={'plan':report['plan'],'approve':True})


def test_plan_apply_noop_and_drift(workspace):
    client, app, captured = workspace
    service = connect(client)['services'][0]
    document = manifest(service['id'])
    before = len(captured)
    report = plan(client,document)
    assert report['has_changes'] and report['changes'][0]['action'] == 'create'
    assert client.get('/api/v1/gateways').json() == []
    assert 'provider-secret-value' not in json.dumps(report)
    assert apply(client,report).status_code == 200
    assert apply(client,report).status_code == 412  # a reviewed create cannot be replayed
    unchanged = plan(client,document)
    assert unchanged['has_changes'] is False
    assert apply(client,unchanged).json()['changes'][0]['version'] == 1
    profile = client.get('/api/v1/gateways').json()[0]
    document['gateways'][0]['config']['max_concurrency'] = 8
    changed = plan(client,document)
    assert changed['changes'][0]['impact'] is not None
    assert apply(client,changed).status_code == 200
    assert plan(client,document)['has_changes'] is False
    assert len(captured) == before
    assert app.state.store.one("SELECT COUNT(*) AS n FROM audit WHERE action='policy-file.applied'")['n'] == 3


def test_stale_dependency_and_whole_plan_atomicity(workspace):
    client, app, _ = workspace
    service = connect(client)['services'][0]
    document = manifest(service['id'],'first','second')
    report = plan(client,document)
    port = client.get('/api/v1/ports').json()[0]
    client.patch('/api/v1/ports/'+port['id'],json={**port,'description':'changed contract'})
    assert apply(client,report).status_code == 412
    assert client.get('/api/v1/gateways').json() == []
    report = plan(client,document)
    original = app.state.save_gateway
    count = 0
    def failed(*args,**kwargs):
        nonlocal count
        count += 1
        if count == 2: raise RuntimeError('simulated write failure')
        return original(*args,**kwargs)
    app.state.save_gateway = failed
    with pytest.raises(RuntimeError): apply(client,report)
    app.state.save_gateway = original
    assert client.get('/api/v1/gateways').json() == []
    assert apply(client,report).status_code == 200


def test_plan_signature_expiry_approval_and_access(workspace):
    client, app, _ = workspace
    service = connect(client)['services'][0]
    report = plan(client,manifest(service['id']))
    assert client.post('/api/v1/policies/apply',json={'plan':report['plan'],'approve':False}).status_code == 422
    assert apply(client,{'plan':report['plan']+'tampered'}).status_code == 422
    assert apply(client,{'plan':'invalid'}).status_code == 422
    from itsdangerous import URLSafeSerializer
    signer = URLSafeSerializer(app.state.vault.session_secret(), salt='pliqra-policy-plan/1')
    payload = signer.loads(report['plan'])
    payload['expires'] = app.state.store.clock() - 1
    assert apply(client,{'plan':signer.dumps(payload)}).status_code == 412
    local_user(client,'Viewer')
    client.post('/api/v1/logout',json={})
    sign_in(client,'viewer@example.test')
    assert client.post('/api/v1/policies/plan',json=manifest(service['id'])).status_code == 403
    assert apply(client,report).status_code == 403


def test_mapping_is_explicit_and_guardrail_drift_blocks_apply(workspace):
    client, app, _ = workspace
    service = connect(client)['services'][0]
    guard = rail(client)
    document = manifest(service['id'])
    document['gateways'][0]['config']['guardrails'] = [{'guardrail_id':'input-policy','stages':['input']}]
    assert client.post('/api/v1/policies/plan',json=document).status_code == 422
    document['mapping']['guardrails'] = {'input-policy':guard['id']}
    report = plan(client,document)
    app.state.store.update('guardrails',guard['id'],{'version':2})
    assert apply(client,report).status_code == 412


from tests.test_scale import replicas


def test_reviewed_plan_is_shared_and_conditionally_applied_once(replicas):
    from concurrent.futures import ThreadPoolExecutor
    a,b,first,second = replicas
    service = connect(a)['services'][0]
    report = plan(a,manifest(service['id']))
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda client:apply(client,report),(a,b)))
    assert sorted(response.status_code for response in responses) == [200,412]
    assert first.state.store.one('SELECT COUNT(*) AS n FROM gateway_profiles')['n'] == 1
    assert second.state.store.one('SELECT version FROM gateway_profiles')['version'] == 1
    assert plan(b,manifest(service['id']))['has_changes'] is False


def test_plan_rejects_other_administrator_and_restore_generation(workspace):
    client,app,_ = workspace
    service = connect(client)['services'][0]
    report = plan(client,manifest(service['id']))
    account = client.post('/api/v1/users',json={'name':'Other admin','email':'other@example.test','password':'other-long-password','role':'admin'})
    assert account.status_code == 201
    client.post('/api/v1/logout',json={})
    sign_in(client,'other@example.test','other-long-password')
    assert apply(client,report).status_code == 403
    report = plan(client,manifest(service['id']))
    from itsdangerous import URLSafeSerializer
    signer = URLSafeSerializer(app.state.vault.session_secret(),salt='pliqra-policy-plan/1')
    payload = signer.loads(report['plan']);payload['generation'] = 'previous-restore-generation'
    assert apply(client,{'plan':signer.dumps(payload)}).status_code == 412
    assert client.get('/api/v1/gateways').json() == []
