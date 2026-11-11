"""Regression evidence for the technical architecture review."""
import asyncio
import base64
import hashlib
import json
import sqlite3
import threading

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException

from pliqra.db import Store
from pliqra.operations import backup_bytes, restore_bytes
from pliqra.security import Vault
from pliqra.connectors import bounded_lines
from pliqra.schema_validation import SchemaExecutor
from pliqra import operations
from concurrent.futures import ThreadPoolExecutor
from tests.test_app import workspace, connect
from tests.test_fabric import gateway, rail, bound_key, invoke


def test_packaged_image_recovery_drill_is_isolated(workspace):
    """Run the same offline restore used by the real Kubernetes acceptance job."""
    import subprocess
    import sys
    from scripts.check_kubernetes_install import RECOVERY_DRILL
    client, app, _ = workspace
    connection = connect(client, api_key='fixture-secret')
    service = connection['services'][0]
    binding = client.post('/protocol/v1/bindings', json={'service': service['name']}).json()
    password = 'synthetic-recovery-drill-password'
    archive = client.post('/api/v1/backups', json={'password': password}).content
    payload = {'profile': 'sqlite', 'archive': base64.b64encode(archive).decode(),
               'password': password, 'connection': connection['id'], 'binding': binding['id']}
    result = subprocess.run([sys.executable, '-c', RECOVERY_DRILL], input=json.dumps(payload),
                            text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert 'Isolated restore verified' in result.stdout
    assert client.get('/api/v1/session').json()['user']['role'] == 'admin'
    assert app.state.vault.decrypt(app.state.store.one('SELECT secret FROM connections')['secret']) == 'fixture-secret'


def swap_upstream(app, handler):
    mock = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.state.gateway.client = mock
    app.state.client = mock
    return mock


@pytest.mark.parametrize('stream', [False, True])
def test_checked_output_has_no_unchecked_auxiliary_channel(workspace, stream):
    client, app, _ = workspace
    service = connect(client)['services'][0]
    guard = rail(client, config={'rule': 'pii', 'pii_types': ['email'], 'action': 'redact'})
    profile = gateway(client, [service['id']], guardrails=[{'guardrail_id': guard['id'], 'stages': ['output']}])
    _, headers = bound_key(client, profile)
    secret = 'private@example.test'
    mock = swap_upstream(app, lambda _: httpx.Response(200, json={
        'id': secret, 'model': secret, 'provider_debug': secret,
        'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': secret},
                     'finish_reason': 'stop', 'logprobs': {'tokens': [secret]}}],
        'usage': {'prompt_tokens': 1, 'completion_tokens': 2, 'debug': secret}}))
    response = invoke(client, service['name'], headers, stream=stream)
    assert response.status_code == 200
    assert '[REDACTED]' in response.text
    assert secret not in response.text
    asyncio.run(mock.aclose())


@pytest.mark.parametrize('extra', [
    {'refusal': {'unchecked': 'private@example.test'}},
    {'reasoning_content': {'unchecked': 'private@example.test'}},
    {'function_call': {'name': 'lookup', 'arguments': {'unchecked': 'private@example.test'}}},
    {'tool_calls': ['private@example.test']},
])
def test_malformed_message_fields_cannot_bypass_output_checks(workspace, extra):
    client, app, _ = workspace
    service = connect(client)['services'][0]
    guard = rail(client, config={'rule': 'pii', 'pii_types': ['email'], 'action': 'redact'})
    profile = gateway(client, [service['id']], guardrails=[{'guardrail_id': guard['id'], 'stages': ['output']}])
    _, headers = bound_key(client, profile)
    mock = swap_upstream(app, lambda _: httpx.Response(200, json={
        'choices': [{'message': {'role': 'assistant', 'content': 'Visible response', **extra}}]}))
    response = invoke(client, service['name'], headers)
    assert response.status_code == 502
    assert 'private@example.test' not in response.text
    assert not app.state.store.all("SELECT * FROM events WHERE status='success'")
    asyncio.run(mock.aclose())


def test_legacy_function_request_cannot_bypass_tool_permission(workspace):
    client, app, captured = workspace
    connection = connect(client); service = connection['services'][0]
    profile = gateway(client, [service['id']], allow_tools=False)
    _, headers = bound_key(client, profile)
    before = len(captured)
    result = invoke(client, service['name'], headers, functions=[{'name': 'external_action', 'parameters': {'type': 'object'}}],
                    function_call={'name': 'external_action'})
    assert result.status_code == 403
    assert len(captured) == before


@pytest.mark.parametrize('extra', [{'max_new_tokens': 100000}, {'max_tokens': 1, 'max_completion_tokens': 9999}, {'stream_options': []}])
def test_ambiguous_or_unknown_generation_controls_fail_before_admission(workspace, extra):
    client, app, captured = workspace
    service = connect(client)['services'][0]
    before = len(captured)
    result = invoke(client, service['name'], **extra)
    assert result.status_code == 422
    assert len(captured) == before
    assert app.state.store.one('SELECT COUNT(*) AS n FROM attempts')['n'] == 0


def test_partial_gateway_usage_never_falls_below_known_tokens(workspace):
    client, app, _ = workspace
    first = connect(client); second = connect(client, endpoint='https://second.test')
    port = client.get('/api/v1/ports').json()[0]
    # Use both deployments in a deterministic priority order.
    deployments = [app.state.gateway.port(first['services'][0]['id'])['deployments'][0], app.state.gateway.port(second['services'][0]['id'])['deployments'][0]]
    client.patch('/api/v1/ports/'+port['id'], json={**port, 'deployments': deployments})
    profile = gateway(client, [port['id']])
    _, headers = bound_key(client, profile)
    def upstream(request):
        if request.url.host != 'second.test':
            return httpx.Response(503, json={'usage': {'prompt_tokens': 20000, 'completion_tokens': 10000}})
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': 'Unknown final usage'}}]})
    mock = swap_upstream(app, upstream)
    assert invoke(client, port['name'], headers).status_code == 200
    ledger = app.state.store.one('SELECT * FROM gateway_requests')
    assert ledger['tokens'] >= 30000
    asyncio.run(mock.aclose())


@pytest.mark.parametrize('stream', [False, True])
def test_event_and_gateway_settlement_commit_together(workspace, monkeypatch, stream):
    client, app, _ = workspace
    service = connect(client)['services'][0]
    from tests.test_app import price
    price(client)
    profile = gateway(client, [service['id']])
    _, headers = bound_key(client, profile)
    def fail(*args):
        raise RuntimeError('Injected settlement failure')
    monkeypatch.setattr(app.state.gateway.policies, 'settle', fail)
    with pytest.raises(RuntimeError, match='Injected settlement failure'):
        invoke(client, service['name'], headers, stream=stream)
    assert app.state.store.one('SELECT status FROM events')['status'] == 'pending'
    assert app.state.store.one('SELECT status FROM gateway_requests')['status'] == 'pending'
    # JSON usage is persisted before possible async output checks; pending streaming
    # usage must roll back together with the event and quota ledger.
    assert app.state.store.one('SELECT outcome FROM attempts')['outcome'] == ('pending' if stream else 'success')


def test_admission_rechecks_service_snapshot(workspace):
    client, app, captured = workspace
    service = connect(client)['services'][0]
    execute = app.state.gateway.execute
    async def changed_after_route(*args, **kwargs):
        port = app.state.gateway.port(service['id'])
        app.state.store.update('ports', port['id'], {'version': port['version']+1, 'max_output_tokens': 1})
        return await execute(*args, **kwargs)
    app.state.gateway.execute = changed_after_route
    before = len(captured)
    result = invoke(client, service['name'], max_tokens=100)
    assert result.status_code == 409
    assert len(captured) == before


def test_admission_rechecks_account_lifecycle(workspace):
    client, app, captured = workspace
    service = connect(client)['services'][0]
    execute = app.state.gateway.execute
    async def revoked_after_route(*args, **kwargs):
        app.state.store.run("UPDATE users SET active=0")
        return await execute(*args, **kwargs)
    app.state.gateway.execute = revoked_after_route
    before = len(captured)
    result = invoke(client, service['name'])
    assert result.status_code == 401
    assert len(captured) == before


@pytest.mark.parametrize('missing', [False, True])
def test_guardrail_credentials_participate_in_vault_key_checks(workspace, monkeypatch, missing):
    client, app, _ = workspace
    rail(client, provider='webhook', api_key='guard-only-secret', config={'endpoint': 'https://check.test/check'})
    directory = app.state.store.directory
    if missing:
        (directory/'encryption.key').unlink()
    else:
        monkeypatch.setenv('PLIQRA_ENCRYPTION_KEY', Fernet.generate_key().decode())
    with pytest.raises(RuntimeError, match='encryption key'):
        Vault(directory)
    if missing:
        assert not (directory/'encryption.key').exists()


def test_restore_rejects_ciphertext_key_mismatch_before_mutation(workspace):
    client, app, _ = workspace
    connect(client)
    password = 'synthetic-backup-password'
    archive = backup_bytes(app.state.store, app.state.vault, password)
    salt = archive[9:25]
    cipher = Fernet(base64.urlsafe_b64encode(hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 600000)))
    payload = json.loads(cipher.decrypt(archive[25:]))
    payload['key'] = Fernet.generate_key().decode()
    corrupt = archive[:25]+cipher.encrypt(json.dumps(payload).encode())
    previous_key = app.state.vault.key
    with pytest.raises(HTTPException) as raised:
        restore_bytes(app.state.store, app.state.vault, corrupt, password)
    assert raised.value.status_code == 422
    assert app.state.vault.key == previous_key
    assert app.state.vault.decrypt(app.state.store.one('SELECT secret FROM connections')['secret']) == 'provider-secret-value'


def test_newer_database_is_rejected_without_downgrade(workspace, tmp_path):
    _, app, _ = workspace
    directory = tmp_path/'future'; directory.mkdir()
    path = directory/'pliqra.sqlite3'
    with sqlite3.connect(path) as connection:
        app.state.store.conn.backup(connection)
        connection.execute('PRAGMA user_version=999')
    with pytest.raises(RuntimeError, match='newer'):
        Store(directory)
    with sqlite3.connect(path) as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 999


@pytest.mark.parametrize('payload', [[], {'choices': [{}]}, {'choices': [{'message': []}]},
    b'{"choices":[{"message":{"role":"assistant","content":"ok"}}],"provider_debug":1e999}',
    b'['*1200+b'0'+b']'*1200])
def test_invalid_completion_shape_is_not_marked_verified(workspace, payload):
    client, app, _ = workspace
    service = connect(client)['services'][0]
    mock = swap_upstream(app, lambda _: httpx.Response(200, **({'content':payload} if isinstance(payload,bytes) else {'json':payload})))
    result = invoke(client, service['name'])
    assert result.status_code == 502
    assert app.state.store.one('SELECT status FROM events')['status'] == 'error'
    assert json.loads(app.state.store.one('SELECT verified FROM deployments')['verified']) == []
    asyncio.run(mock.aclose())


class LongLine(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'data: {"choices":[{"delta":{"content":"'
        for _ in range(20):
            yield b'a'*65536
        yield b'"}}]}\n\ndata: [DONE]\n\n'


def test_stream_frame_size_is_bounded_and_failure_is_explicit(workspace):
    client, app, _ = workspace
    service = connect(client)['services'][0]
    mock = swap_upstream(app, lambda _: httpx.Response(200, stream=LongLine()))
    result = invoke(client, service['name'], stream=True)
    assert 'upstream_stream_error' in result.text
    assert '[DONE]' not in result.text
    assert app.state.store.one('SELECT status FROM events')['status'] == 'incomplete'
    asyncio.run(mock.aclose())


@pytest.mark.parametrize('action',['block','redact'])
def test_keyword_checks_do_not_match_or_rewrite_protocol_roles(workspace, action):
    client,app,captured=workspace
    service=connect(client)['services'][0]
    guard=rail(client,config={'rule':'keywords','terms':['user'],'action':action})
    profile=gateway(client,[service['id']],guardrails=[{'guardrail_id':guard['id'],'stages':['input']}])
    _,headers=bound_key(client,profile)
    response=invoke(client,service['name'],headers,content='Ordinary content')
    assert response.status_code==200
    assert json.loads(captured[-1].content)['messages'][0]['role']=='user'


def test_small_stream_frame_is_delivered_without_transport_aggregation():
    class FirstFrame(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[]}\n\n'
            raise AssertionError('Parser consumed later transport data before delivering its first frame')
    async def check():
        response=httpx.Response(200,stream=FirstFrame())
        lines=bounded_lines(response)
        assert await anext(lines)=='data: {"choices":[]}'
        await lines.aclose()
        await response.aclose()
    asyncio.run(check())


def test_schema_timeout_leaves_event_loop_responsive_and_reaps_child(monkeypatch):
    children=[]
    create=asyncio.create_subprocess_exec
    async def capture(*args,**kwargs):
        process=await create(*args,**kwargs)
        children.append(process)
        return process
    monkeypatch.setattr(asyncio,'create_subprocess_exec',capture)
    async def check():
        executor=SchemaExecutor()
        ticks=0
        async def heartbeat():
            nonlocal ticks
            while True:
                ticks+=1
                await asyncio.sleep(.01)
        pulse=asyncio.create_task(heartbeat())
        try:
            with pytest.raises(TimeoutError):
                await executor.validate({'type':'string','pattern':'^(a+)+$'},'a'*10000+'!',timeout=.35)
        finally:
            pulse.cancel()
            await asyncio.gather(pulse,return_exceptions=True)
        assert ticks>=5
        assert children and all(p.returncode is not None for p in children)
    asyncio.run(check())


@pytest.mark.parametrize('change',['session','evidence'])
def test_admission_rechecks_session_and_verified_eligibility(workspace, change):
    client,app,captured=workspace
    service=connect(client)['services'][0]
    if change=='evidence':
        row=app.state.store.one('SELECT id FROM deployments')
        app.state.store.update('deployments',row['id'],{'verified':'["chat"]'})
        port=app.state.gateway.port(service['id'])
        client.patch('/api/v1/ports/'+port['id'],json={**port,'verified_only':True})
    execute=app.state.gateway.execute
    async def changed_after_route(*args,**kwargs):
        if change=='session':
            app.state.store.run('DELETE FROM sessions')
        else:
            app.state.store.run("UPDATE deployments SET verified='[]'")
        return await execute(*args,**kwargs)
    app.state.gateway.execute=changed_after_route
    before=len(captured)
    assert invoke(client,service['name']).status_code==(401 if change=='session' else 503)
    assert len(captured)==before


def service_with_fallback(client,app,**options):
    first=connect(client);second=connect(client,endpoint='https://second.test')
    a=app.state.gateway.port(first['services'][0]['id'])['deployments'][0]
    b=app.state.gateway.port(second['services'][0]['id'])['deployments'][0]
    app.state.store.update('deployments',b,{'model':'second-quartz'})
    port=app.state.gateway.port(first['services'][0]['id'])
    response=client.patch('/api/v1/ports/'+port['id'],json={**port,'deployments':[a,b],**options})
    assert response.status_code==200
    return port,a,b


def test_concurrent_round_robin_selection_happens_at_atomic_admission(workspace):
    client,app,_=workspace
    port,a,b=service_with_fallback(client,app,strategy='round_robin')
    execute=app.state.gateway.execute
    waiting=0
    ready=None
    async def prepared_together(*args,**kwargs):
        nonlocal waiting,ready
        if ready is None:ready=asyncio.Event()
        waiting+=1
        if waiting==2:ready.set()
        await asyncio.wait_for(ready.wait(),3)
        return await execute(*args,**kwargs)
    app.state.gateway.execute=prepared_together
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:invoke(client,port['name']).status_code,range(2)))
    assert results==[200,200]
    assert {r['deployment_id'] for r in app.state.store.all('SELECT deployment_id FROM events')}=={a,b}


def test_cached_fallback_retains_actual_deployment_attribution(workspace):
    client,app,_=workspace
    port,a,b=service_with_fallback(client,app,cache_ttl=60)
    calls=[]
    def upstream(request):
        calls.append(request)
        if request.url.host!='second.test':return httpx.Response(503)
        return httpx.Response(200,json={'model':'second-quartz','choices':[{'message':{'role':'assistant','content':'Cached answer'}}],
                                      'usage':{'prompt_tokens':1,'completion_tokens':1}})
    mock=swap_upstream(app,upstream)
    first=invoke(client,port['name']);assert first.status_code==200
    before=len(calls)
    cached=invoke(client,port['name']);assert cached.status_code==200
    assert len(calls)==before and cached.headers['x-pliqra-cache']=='HIT'
    assert cached.headers['x-pliqra-model']=='second-quartz'
    event=app.state.store.one('SELECT * FROM events WHERE id=?',(cached.headers['x-request-id'],))
    assert event['deployment_id']==b and event['model']=='second-quartz'
    assert event['input_tokens']==event['output_tokens']==event['cost_micros']==0
    asyncio.run(mock.aclose())


@pytest.mark.parametrize('mutation',['connection','workspace'])
@pytest.mark.parametrize('failure',[False,True])
def test_health_results_cannot_cross_configuration_or_restore_boundary(workspace,monkeypatch,mutation,failure):
    client,app,_=workspace
    connection=connect(client)
    before=app.state.store.one('SELECT * FROM connections WHERE id=?',(connection['id'],))
    async def stale(*args,**kwargs):
        if mutation=='connection':
            app.state.store.update('connections',connection['id'],{'endpoint':'https://new.test/v1'})
        else:
            app.state.gateway.generation+=1
        if failure:raise HTTPException(422,'Synthetic discovery failure')
        return {'provider':'openai','endpoint':'https://old.test/v1','models':[{'model':'stale-model','capabilities':['chat']}]}
    monkeypatch.setattr(operations,'discover',stale)
    assert client.post('/api/v1/connections/'+connection['id']+'/check',json={}).status_code==409
    after=app.state.store.one('SELECT * FROM connections WHERE id=?',(connection['id'],))
    assert after['last_checked']==before['last_checked'] and after['status']==before['status']
    assert not app.state.store.one("SELECT id FROM deployments WHERE model='stale-model'")


def test_restore_waits_for_explicit_guardrail_checks(workspace):
    client,app,_=workspace
    guard=rail(client,provider='webhook',config={'endpoint':'https://check.test/check'})
    archive=base64.b64encode(backup_bytes(app.state.store,app.state.vault,'synthetic-backup-password')).decode()
    started=threading.Event();release=threading.Event()
    async def slow(request):
        started.set()
        while not release.is_set():await asyncio.sleep(.01)
        return httpx.Response(200,json={'allow':True})
    mock=swap_upstream(app,slow)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(client.post,'/api/v1/guardrails/'+guard['id']+'/test',json={'text':'Synthetic check'})
        try:
            assert started.wait(3)
            response=client.post('/api/v1/backups/restore',json={'archive':archive,'password':'synthetic-backup-password','confirmation':'RESTORE'})
            assert response.status_code==409
        finally:
            release.set()
        assert future.result(timeout=3).status_code==200
    assert app.state.gateway.checks==0
    asyncio.run(mock.aclose())


def test_named_gateway_catalog_and_inference_are_not_cacheable(workspace):
    client,app,_=workspace
    service=connect(client)['services'][0]
    profile=gateway(client,[service['id']])
    _,headers=bound_key(client,profile)
    assert client.get('/g/finance/v1/models',headers=headers).headers['cache-control']=='no-store'
    assert invoke(client,service['name'],headers,path='/g/finance/v1/chat/completions').headers['cache-control']=='no-store'


def test_nonfinite_values_in_nested_controls_fail_before_admission(workspace):
    client,app,captured=workspace
    service=connect(client)['services'][0]
    before=len(captured)
    payload={'model':service['name'],'messages':[{'role':'user','content':'Synthetic request'}],
             'tools':[{'type':'function','function':{'name':'x','parameters':{'default':float('nan')}}}]}
    response=client.post('/v1/chat/completions',content=json.dumps(payload),headers={'Content-Type':'application/json'})
    assert response.status_code==422 and len(captured)==before
    assert app.state.store.one('SELECT COUNT(*) AS n FROM attempts')['n']==0


def test_discovery_stops_reading_before_loading_an_oversized_body(workspace):
    client,app,_=workspace
    streams=[]
    class Huge(httpx.AsyncByteStream):
        read=0
        async def __aiter__(self):
            for _ in range(64):
                self.read+=65536
                yield b' '*65536
    def upstream(request):
        stream=Huge();streams.append(stream)
        return httpx.Response(200,stream=stream)
    mock=swap_upstream(app,upstream)
    result=client.post('/api/v1/connections',json={'name':'Huge discovery','endpoint':'https://large.test','provider':'openai'})
    assert result.status_code==422
    assert streams and all(s.read<=2_000_000+65536 for s in streams)
    asyncio.run(mock.aclose())


def test_inference_implementation_revision_invalidates_old_binding(workspace,monkeypatch):
    from pliqra import gateway as implementation
    client,app,captured=workspace
    service=connect(client)['services'][0]
    binding=client.post('/protocol/v1/bindings',json={'service':service['name']}).json()
    before=len(captured)
    monkeypatch.setattr(implementation,'INFERENCE_CONTRACT_REVISION','synthetic-future-revision')
    assert invoke(client,service['name'],path=binding['invoke']).status_code==409
    assert len(captured)==before
