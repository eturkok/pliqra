"""Publication impact and existing-client contract protection regressions."""
import json

from tests.test_app import workspace, connect, key
from tests.test_fabric import gateway, rail, invoke
from pliqra.db import now


def application(client, service, requirements=None, **options):
    credential = key(client, project_id='default', **options)
    headers = {'Authorization': 'Bearer ' + credential['token']}
    result = client.post('/protocol/v1/bindings', headers=headers,
                         json={'service': service['name'], 'requirements': requirements or ['chat']})
    assert result.status_code == 201, result.text
    return credential, headers | {'X-Pliqra-Binding': result.json()['id']}, result.json()


def test_header_protection_streaming_identity_expiry_and_explicit_renewal(workspace):
    client, app, captured = workspace
    connect(client)
    service = client.get('/api/v1/ports').json()[0]
    credential, headers, binding = application(client, service)
    for path in ['/v1/chat/completions']:
        response = invoke(client, service['name'], headers, path)
        assert response.status_code == 200
        assert response.headers['X-Pliqra-Binding'] == binding['id']
        stream = invoke(client, service['name'], headers, path, stream=True)
        assert stream.status_code == 200 and '[DONE]' in stream.text
    assert 'x-pliqra-binding' not in captured[-1].headers
    before = len(captured)
    other = key(client, project_id='default')
    assert invoke(client, service['name'], headers | {'Authorization': 'Bearer ' + other['token']}).status_code == 403
    app.state.store.update('bindings', binding['id'], {'expires': now() - 1})
    assert invoke(client, service['name'], headers).status_code == 403
    app.state.store.update('bindings', binding['id'], {'expires': now() + 3600})
    body = {'name': service['name'], 'deployments': service['deployments'], 'requirements': ['chat']}
    assert client.patch('/api/v1/ports/' + service['id'], json=body).status_code == 200
    assert invoke(client, service['name'], headers).status_code == 409
    assert len(captured) == before
    renewed = client.post('/protocol/v1/bindings', headers={'Authorization': 'Bearer ' + credential['token']},
                          json={'service': service['name']}).json()
    assert invoke(client, service['name'], headers | {'X-Pliqra-Binding': renewed['id']}).status_code == 200
    assert invoke(client, service['name'], {'Authorization': 'Bearer ' + credential['token']}).status_code == 200


def test_header_never_silently_ignored_and_gateway_policy_still_applies(workspace):
    client, app, captured = workspace
    connect(client)
    service = client.get('/api/v1/ports').json()[0]
    profile = gateway(client, [service['id']], limits=[{'metric': 'requests', 'limit': 1}])
    _, headers, binding = application(client, service, gateway_id=profile['id'])
    assert invoke(client, service['name'], headers, '/g/finance/v1/chat/completions').status_code == 200
    assert invoke(client, service['name'], headers).status_code == 429
    before = len(captured)
    for value in ['', 'bad', binding['id'] + ',' + binding['id']]:
        assert invoke(client, service['name'], headers | {'X-Pliqra-Binding': value}).status_code == 422
    duplicate = list(headers.items()) + [('X-Pliqra-Binding', binding['id'])]
    assert invoke(client, service['name'], duplicate).status_code == 422
    assert client.post('/v1/responses', headers=headers, json={'model': service['name'], 'input': 'test'}).status_code == 422
    assert invoke(client, service['name'], headers | {'X-Pliqra-Binding': 'bind_missing'}).status_code == 403
    assert len(captured) == before


def test_service_impact_reports_requirements_teams_access_and_pagination(workspace):
    client, app, captured = workspace
    connect(client)
    service = client.get('/api/v1/ports').json()[0]
    deployment = service['deployments'][0]
    app.state.store.update('deployments', deployment, {'capabilities': json.dumps(['chat', 'streaming', 'tools'])})
    app.state.store.insert('teams', {'id': 'team_support', 'name': 'Support', 'created': now()})
    app.state.store.insert('team_projects', {'team_id': 'team_support', 'project_id': 'default', 'role': 'member'})
    _, _, plain = application(client, service, ['chat'], name='Summary')
    _, _, tools = application(client, service, ['tools'], name='Support bot', team_id='team_support')
    revoked, _, _ = application(client, service, name='Revoked')
    client.delete('/api/v1/keys/' + revoked['id'])
    expired, _, expiring = application(client, service, name='Expired binding')
    app.state.store.update('bindings', expiring['id'], {'expires': 0})
    connected = connect(client)['services'][0]
    candidate = next(item for item in client.get('/api/v1/ports').json() if item['id'] == connected['id'])
    app.state.store.update('deployments', candidate['deployments'][0], {'capabilities': '["chat"]'})
    proposed = {'name': service['name'], 'deployments': candidate['deployments'], 'requirements': ['chat']}
    path = '/api/v1/ports/' + service['id'] + '/preview'
    before = len(captured)
    report = client.post(path, json=proposed).json()
    assert report['impact']['summary'] == {'renew': 1, 'blocked': 1, 'unavailable': 0}
    row = next(item for item in report['impact']['items'] if item['binding_id'] == tools['id'])
    assert row['application'] == 'Support bot' and row['team']['name'] == 'Support'
    assert row['project']['name'] == 'Default workspace' and row['requirements'] == ['tools']
    assert 'tools' in str(row['reasons'])
    assert report['impact']['excluded_inactive_or_unauthorized'] == 1
    page = client.post(path + '?limit=1&offset=1', json=proposed).json()['impact']
    assert len(page['items']) == 1 and page['total'] == 2
    assert len(captured) == before and app.state.store.one('SELECT COUNT(*) AS n FROM gateway_requests')['n'] == 0
    # A preview cannot overwrite a service changed by another administrator.
    assert client.patch('/api/v1/ports/' + service['id'], json=proposed).status_code == 200
    stale = client.patch('/api/v1/ports/' + service['id'], headers={'If-Match': '"' + report['current_fingerprint'] + '"'}, json=proposed)
    assert stale.status_code == 412


def test_policy_impact_and_header_detect_guardrail_changes(workspace):
    client, app, captured = workspace
    connect(client)
    service = client.get('/api/v1/ports').json()[0]
    app.state.store.update('deployments', service['deployments'][0], {'capabilities': '["chat","tools"]'})
    check = rail(client)
    profile = gateway(client, [service['id']], guardrails=[{'guardrail_id': check['id']}])
    _, headers, _ = application(client, service, ['tools'], gateway_id=profile['id'])
    document = {'name': profile['name'], 'slug': profile['slug'], 'config': {**profile['config'], 'allow_tools': False}}
    before = len(captured)
    report = client.post('/api/v1/gateways/' + profile['id'] + '/impact', json=document).json()
    assert report['impact']['summary']['blocked'] == 1
    assert len(captured) == before
    app.state.store.update('guardrails', check['id'], {'version': 2})
    assert invoke(client, service['name'], headers).status_code == 409
    assert client.put('/api/v1/gateways/' + profile['id'], headers={'If-Match': '"' + report['current_fingerprint'] + '"'}, json=document).status_code == 412
    assert len(captured) == before
