"""Scheduled behavior observations, limits, lifecycle and recovery."""
import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from tests.test_app import workspace, connect, Fragments, price
from pliqra.behavior import claim, execute


def schedule(client, deployment, **fields):
    body = {'name': 'Stable endpoint checks', 'deployment_id': deployment, 'enabled': False,
            'checks': [{'name': name, 'capability': capability} for name, capability in
                       [('Chat','chat'),('Stream','streaming'),('JSON','structured_output'),('Tool','tools')]], **fields}
    response = client.post('/api/v1/behavior-checks', json=body)
    assert response.status_code == 201, response.text
    return response.json()


def definition(row, **fields):
    keys = ['name','deployment_id','project_id','enabled','interval_seconds','max_runs_per_day','max_tokens','timeout_seconds','checks']
    return {**{key: row[key] for key in keys}, **fields}


def completed(client, schedule_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        rows = client.get('/api/v1/behavior-checks/' + schedule_id + '/runs').json()['items']
        if rows and rows[0]['status'] != 'running':
            return rows[0]
        time.sleep(.01)
    raise AssertionError('Behavior check did not finish')


def run(client, schedule_id):
    response = client.post('/api/v1/behavior-checks/' + schedule_id + '/run', json={})
    assert response.status_code == 202, response.text
    return completed(client, schedule_id)


@pytest.fixture
def observed(workspace):
    client, app, _ = workspace
    connect(client)
    deployment = client.get('/api/v1/connections').json()[0]['models'][0]
    control = {'mode': 'ok', 'calls': 0, 'started': threading.Event(), 'release': threading.Event()}

    async def upstream(request):
        control['calls'] += 1
        body = json.loads(request.content)
        if control['mode'] == 'wait':
            control['started'].set()
            while not control['release'].is_set():
                await asyncio.sleep(.01)
        if control['mode'] == 'unreachable':
            return httpx.Response(503)
        usage = {'prompt_tokens': 8, 'completion_tokens': 4}
        if body.get('stream'):
            payload = 'data: {"choices":[{"delta":{"content":"OK"}}]}\n\n'
            payload += 'data: ' + json.dumps({'choices': [], 'usage': usage}) + '\n\ndata: [DONE]\n\n'
            return httpx.Response(200, stream=Fragments(payload))
        message = {'role': 'assistant', 'content': 'OK'}
        if body.get('response_format'):
            message['content'] = '{"ok":true}'
        if body.get('tools'):
            value = 'wrong' if control['mode'] == 'changed' else 'ok'
            message = {'role':'assistant','content':None,'tool_calls':[{'id':'call_1','type':'function',
                       'function':{'name':'pliqra_probe','arguments':json.dumps({'value':value})}}]}
        return httpx.Response(200, json={'choices':[{'message':message,'finish_reason':'stop'}], 'usage':usage})

    mock = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    app.state.gateway.client = mock
    yield client, app, deployment, control
    asyncio.run(mock.aclose())


def test_same_endpoint_transition_and_last_success_without_routing_mutation(observed):
    client, app, deployment, control = observed
    row = schedule(client, deployment['id'])
    before = app.state.store.one('SELECT * FROM deployments WHERE id=?', (deployment['id'],))
    first = run(client, row['id'])
    assert first['status'] == 'passed' and len(first['results']) == 4
    previous = {item['name']: item['last_success_at'] for item in first['results']}
    control['mode'] = 'changed'
    degraded = run(client, row['id'])
    assert degraded['status'] == 'failed'
    tool = next(item for item in degraded['results'] if item['name'] == 'Tool')
    assert tool['changed'] and tool['last_success_at'] == previous['Tool']
    assert next(item for item in degraded['results'] if item['name']=='Chat')['passed']
    notes = client.get('/api/v1/operations').json()['notifications']
    assert len([note for note in notes if note['kind']=='model-behavior']) == 1
    repeated = run(client, row['id'])
    repeated_tool = next(item for item in repeated['results'] if item['name']=='Tool')
    assert not repeated_tool['changed'] and repeated_tool['last_change_at']==tool['tested_at']
    assert repeated_tool['previous_outcome']=='passed'
    assert app.state.store.one("SELECT COUNT(*) AS n FROM notifications WHERE kind='model-behavior'")['n'] == 1
    control['mode'] = 'ok'
    recovered = run(client, row['id'])
    assert recovered['status'] == 'passed'
    assert app.state.store.one("SELECT COUNT(*) AS n FROM notifications WHERE kind='model-behavior'")['n'] == 2
    after = app.state.store.one('SELECT * FROM deployments WHERE id=?', (deployment['id'],))
    for field in ('capabilities','verified','verified_at','failure_count','open_until'):
        assert after[field] == before[field], field
    events = client.get('/api/v1/events', params={'request_kind':'probe'}).json()
    assert events['total'] == 16
    assert all(event['project_id']=='default' for event in events['items'])
    assert not app.state.store.one('SELECT id FROM event_content LIMIT 1')


def test_atomic_cycle_claim_daily_limit_and_no_catch_up(observed):
    client, app, deployment, control = observed
    row = schedule(client, deployment['id'], enabled=True, max_runs_per_day=1,
                   checks=[{'name':'Chat','capability':'chat'}])
    app.state.store.update('behavior_schedules', row['id'], {'next_run': app.state.store.clock()-10000})
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: claim(app), range(2)))
    assert sum(item is not None for item in claims) == 1
    record = next(item for item in claims if item)
    asyncio.run_coroutine_threadsafe(execute(app, record), app.state.loop).result(5)
    assert control['calls'] == 1
    app.state.store.update('behavior_schedules', row['id'], {'next_run': app.state.store.clock()-1})
    assert claim(app) is None
    response = client.post('/api/v1/behavior-checks/' + row['id'] + '/run', json={})
    assert response.status_code == 429
    current = client.get('/api/v1/behavior-checks').json()['items'][0]
    assert current['daily_runs'] == 1 and current['next_run'] > app.state.store.clock()
    assert control['calls'] == 1


def test_project_budget_error_is_distinct_from_behavior_failure(observed):
    client, app, deployment, control = observed
    price(client)
    project = client.post('/api/v1/projects', json={'name':'Check budget','monthly_budget':0}).json()
    row = schedule(client, deployment['id'], project_id=project['id'])
    result = run(client, row['id'])
    assert result['status'] == 'error' and control['calls'] == 0
    assert all(item['outcome']=='error' and '402' in item['detail'] for item in result['results'])
    assert all(item['last_success_at'] is None for item in result['results'])


def test_pausing_during_a_cycle_stops_remaining_calls_and_preserves_new_config(observed):
    client, app, deployment, control = observed
    row = schedule(client, deployment['id'], enabled=True)
    control['mode'] = 'wait'
    assert client.post('/api/v1/behavior-checks/' + row['id'] + '/run', json={}).status_code == 202
    assert control['started'].wait(2)
    updated = client.put('/api/v1/behavior-checks/' + row['id'], json=definition(row, enabled=False))
    assert updated.status_code == 200
    control['release'].set()
    result = completed(client, row['id'])
    assert result['status'] == 'superseded' and control['calls'] == 1
    current = client.get('/api/v1/behavior-checks').json()['items'][0]
    assert current['enabled'] is False and current['last_status'] == 'paused'
    assert not current['last_results']


def test_custom_assertion_and_provider_error_do_not_change_deployment_health(observed):
    client, app, deployment, control = observed
    row = schedule(client, deployment['id'], checks=[{'name':'Known response','capability':'chat',
                   'prompt':'Return a specific sentinel','contains':'EXPECTED_SENTINEL'}])
    result = run(client, row['id'])
    assert result['status'] == 'failed' and control['calls'] == 1
    config = app.state.store.one('SELECT config FROM behavior_schedules WHERE id=?', (row['id'],))['config']
    assert 'EXPECTED_SENTINEL' not in config
    assert json.loads(app.state.vault.decrypt(config))['checks'][0]['contains']=='EXPECTED_SENTINEL'
    control['mode'] = 'unreachable'
    for _ in range(3):
        assert run(client, row['id'])['status'] == 'error'
    current = app.state.store.one('SELECT failure_count,open_until FROM deployments WHERE id=?', (deployment['id'],))
    assert current['failure_count'] == 0 and current['open_until'] == 0


def test_timeout_and_revoked_owner_are_visible_without_automatic_replay(observed, monkeypatch):
    client, app, deployment, control = observed
    row = schedule(client, deployment['id'], enabled=True, checks=[{'name':'Chat','capability':'chat'}])
    async def timeout(*args, **kwargs):
        raise TimeoutError()
    monkeypatch.setattr('pliqra.behavior.capability_probe', timeout)
    result = run(client, row['id'])
    assert result['status'] == 'error' and 'timed out' in result['results'][0]['detail']
    app.state.store.update('users', row['actor'], {'active':0})
    app.state.store.update('behavior_schedules', row['id'], {'next_run':app.state.store.clock()-1})
    assert claim(app) is None
    current = app.state.store.one('SELECT last_status,last_error FROM behavior_schedules WHERE id=?', (row['id'],))
    assert current['last_status']=='error' and 'administrator' in current['last_error']
    assert control['calls'] == 0


def test_worker_runs_only_enabled_due_schedules(observed):
    client, app, deployment, control = observed
    row = schedule(client, deployment['id'], checks=[{'name':'Chat','capability':'chat'}])
    app.state.store.update('behavior_schedules', row['id'], {'next_run':app.state.store.clock()-1})
    assert claim(app) is None
    enabled = client.put('/api/v1/behavior-checks/' + row['id'], json=definition(row, enabled=True)).json()
    assert enabled['next_run'] > app.state.store.clock()
    assert claim(app) is None
    app.state.store.update('behavior_schedules', row['id'], {'next_run':app.state.store.clock()-1})
    deadline = time.monotonic() + 8
    while time.monotonic()<deadline and not control['calls']:
        time.sleep(.02)
    assert control['calls'] == 1
    assert completed(client, row['id'])['status'] == 'passed'


def test_schedule_api_is_admin_only_and_maintenance_blocks_invocation(observed):
    client, app, deployment, control = observed
    row = schedule(client, deployment['id'])
    app.state.gateway.maintenance = True
    response = client.post('/api/v1/behavior-checks/' + row['id'] + '/run', json={})
    assert response.status_code == 503
    assert control['calls'] == 0
    app.state.gateway.maintenance = False
    user = client.post('/api/v1/users', json={'name':'Viewer','email':'viewer@example.test',
                       'password':'long-viewer-password','role':'viewer'}).json()
    assert user['id']
    login = client.post('/api/v1/login', json={'email':'viewer@example.test','password':'long-viewer-password'}).json()
    client.headers['X-Pliqra-CSRF'] = login['csrf']
    assert client.get('/api/v1/behavior-checks').status_code == 403
    assert client.post('/api/v1/behavior-checks/' + row['id'] + '/run', json={}).status_code == 403
    assert control['calls'] == 0


def test_configuration_change_during_last_check_is_superseded(observed):
    client, app, deployment, control = observed
    row = schedule(client, deployment['id'], checks=[{'name':'Chat','capability':'chat'}])
    control['mode'] = 'wait'
    client.post('/api/v1/behavior-checks/' + row['id'] + '/run', json={})
    assert control['started'].wait(2)
    app.state.store.update('deployments', deployment['id'], {'metadata_hash':'changed-while-checking'})
    control['release'].set()
    result = completed(client, row['id'])
    assert result['status'] == 'superseded'
    current = client.get('/api/v1/behavior-checks').json()['items'][0]
    assert current['last_success_at'] is None
    assert not app.state.store.one("SELECT id FROM notifications WHERE kind='model-behavior' LIMIT 1")


def test_disabled_deployment_is_an_execution_error_not_a_model_regression(observed):
    client, app, deployment, control = observed
    app.state.store.update('deployments', deployment['id'], {'enabled':0})
    row = schedule(client, deployment['id'], checks=[{'name':'Chat','capability':'chat'}])
    report = run(client, row['id'])
    assert report['status']=='error' and '409' in report['error']
    assert not report['results'] and control['calls']==0


def test_deleted_deployment_is_reported_instead_of_leaving_a_stale_pass(observed):
    client, app, deployment, control = observed
    row = schedule(client, deployment['id'], enabled=True, checks=[{'name':'Chat','capability':'chat'}])
    assert run(client, row['id'])['status']=='passed'
    app.state.store.run('DELETE FROM deployments WHERE id=?', (deployment['id'],))
    app.state.store.update('behavior_schedules', row['id'], {'next_run':app.state.store.clock()-1})
    record = claim(app)
    assert record is not None
    asyncio.run_coroutine_threadsafe(execute(app,record), app.state.loop).result(5)
    current = client.get('/api/v1/behavior-checks').json()['items'][0]
    assert current['last_status']=='error' and '404' in current['last_error']
    assert current['deployment'] is None and current['last_success_at'] is None
    assert control['calls']==1


def test_schema_six_sqlite_backup_migrates_and_restored_schedules_are_paused(observed, tmp_path):
    import base64
    import hashlib
    import sqlite3
    from cryptography.fernet import Fernet
    from pliqra.db import SCHEMA_VERSION
    client, app, deployment, control = observed
    row = schedule(client, deployment['id'], enabled=True)
    password = 'behavior-backup-password'
    response = client.post('/api/v1/backups', json={'password':password})
    assert response.status_code==200
    archive = response.content
    assert 'Stable endpoint checks' not in archive.decode(errors='ignore')
    salt = archive[9:25]
    cipher = Fernet(base64.urlsafe_b64encode(hashlib.pbkdf2_hmac('sha256',password.encode(),salt,600000)))
    payload = json.loads(cipher.decrypt(archive[25:]))
    copy = tmp_path/'old.sqlite3'
    copy.write_bytes(base64.b64decode(payload['database']))
    with sqlite3.connect(copy) as connection:
        connection.execute('DROP TABLE behavior_runs')
        connection.execute('DROP TABLE behavior_schedules')
        connection.execute('PRAGMA user_version=6')
    connection.close()
    payload['schema']=6
    payload['database']=base64.b64encode(copy.read_bytes()).decode()
    legacy = b'PLQBACK01'+salt+cipher.encrypt(json.dumps(payload).encode())
    from pliqra.operations import restore_bytes
    restore_bytes(app.state.store,app.state.vault,legacy,password)
    assert app.state.store.one('PRAGMA user_version')['user_version']==SCHEMA_VERSION
    assert app.state.store.one('SELECT COUNT(*) AS n FROM behavior_schedules')['n']==0
    restore_bytes(app.state.store,app.state.vault,archive,password)
    restored = app.state.store.one('SELECT enabled,last_status FROM behavior_schedules WHERE id=?',(row['id'],))
    assert restored['enabled']==0 and restored['last_status']=='paused'
    assert control['calls']==0
