"""Reproductions found during the end-to-end architecture review."""
import asyncio
import json
import io
from urllib.error import HTTPError

import httpx
import pytest

from tests.test_app import workspace
from tests.test_architecture import swap_upstream
from tests.test_fabric import bound_key, gateway, invoke, rail
from tests.test_model_operations import capable
from tests.test_app import Fragments
from sdk.pliqra_client import Connection, Pliqra, PliqraError


def test_password_change_during_login_verification_cannot_create_a_session(workspace, monkeypatch):
    from pliqra import main
    from pliqra.security import hash_password
    client, app, _ = workspace
    original=main.verify_password
    replacement=hash_password('replacement-local-password')

    def password_changed_after_verification(password, stored):
        verified=original(password,stored)
        if verified:
            with app.state.store.transaction():
                app.state.store.run('UPDATE users SET password_hash=? WHERE issuer=?',(replacement,'local'))
                app.state.store.run('DELETE FROM sessions')
        return verified

    monkeypatch.setattr(main,'verify_password',password_changed_after_verification)
    result=client.post('/api/v1/login',json={'email':'owner@example.test','password':'long-local-password'})
    assert result.status_code==401
    assert app.state.store.one('SELECT COUNT(*) AS n FROM sessions')['n']==0


@pytest.mark.parametrize('path', ['/v1/models', '/protocol/v1/services', '/metrics', '/scim/v2/missing/Users'])
def test_bearer_rejections_include_a_challenge_and_are_not_cacheable(workspace, path):
    client, _, _ = workspace
    result=client.get(path,headers={'Authorization':'Bearer invalid-token'})
    assert result.status_code==401
    assert result.headers['WWW-Authenticate']=='Bearer realm="pliqra"'
    assert result.headers['Cache-Control']=='no-store'


def test_authenticated_metrics_are_not_cacheable(workspace):
    client, _, _ = workspace
    token=client.post('/api/v1/monitoring-token',json={}).json()['token']
    result=client.get('/metrics',headers={'Authorization':'Bearer '+token})
    assert result.status_code==200 and result.headers['Cache-Control']=='no-store'


@pytest.mark.parametrize('scheme',['bearer','BeArEr'])
def test_bearer_case_cannot_fall_back_to_an_admin_cookie(workspace,scheme):
    client, app, _=workspace
    service=capable(client)
    profile=gateway(client,[service['id']],limits=[{'metric':'requests','limit':1}])
    token,_=bound_key(client,profile)
    auth={'Authorization':scheme+' '+token['token']}
    assert invoke(client,service['name'],auth).status_code==200
    assert invoke(client,service['name'],auth).status_code==429
    assert app.state.store.one("SELECT key_id FROM events WHERE status='success'")['key_id']==token['id']
    monitoring=client.post('/api/v1/monitoring-token',json={}).json()['token']
    assert client.get('/metrics',headers={'Authorization':scheme+' '+monitoring}).status_code==200


def test_explicit_invalid_authentication_does_not_fall_back_to_a_cookie(workspace):
    client,_,_=workspace
    assert client.get('/v1/models',headers={'Authorization':'Basic ignored'}).status_code==401


def test_json_media_type_tokens_are_case_insensitive(workspace):
    client,_,_=workspace
    service=capable(client)
    assert invoke(client,service['name'],{'Content-Type':'Application/JSON; charset=UTF-8'}).status_code==200


def test_redaction_does_not_reprocess_its_own_marker(workspace):
    client, app, _ = workspace
    service = capable(client)
    guard = rail(client, config={'rule': 'keywords', 'terms': ['secret', 'e', 'e', 'e'], 'action': 'redact'})
    profile = gateway(client, [service['id']], guardrails=[{'guardrail_id': guard['id'], 'stages': ['input']}])
    _, auth = bound_key(client, profile)
    sent = []

    def upstream(request):
        sent.append(json.loads(request.content)['messages'][0]['content'])
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}]})

    mock = swap_upstream(app, upstream)
    try:
        assert invoke(client, service['name'], auth, content='secret').status_code == 200
        assert sent == ['[REDACTED]']
    finally:
        asyncio.run(mock.aclose())


def test_redaction_growth_is_bounded_before_allocating_the_result(workspace, monkeypatch):
    from pliqra import policies
    client, app, sent=workspace
    service=capable(client)
    guard=rail(client,config={'rule':'keywords','terms':['x'],'action':'redact'})
    profile=gateway(client,[service['id']],guardrails=[{'guardrail_id':guard['id'],'stages':['input']}])
    _,auth=bound_key(client,profile)
    monkeypatch.setattr(policies,'MAX_CHECKED_BYTES',200)
    before=len(sent)
    result=invoke(client,service['name'],auth,content='x'*100)
    assert result.status_code==503 and result.json()['detail']['code']=='guardrail_unavailable'
    assert len(sent)==before
    assert app.state.store.one('SELECT COUNT(*) AS n FROM attempts')['n']==0


@pytest.mark.parametrize('limit,expected', [('tokens', 429), ('budget', 402)])
def test_parallel_redaction_reservations_do_not_oversubscribe(workspace, limit, expected):
    from concurrent.futures import ThreadPoolExecutor
    client, app, _ = workspace
    service = capable(client)
    if limit == 'budget':
        assert client.patch('/api/v1/projects/default', json={'name': 'Default', 'monthly_budget': 0.15}).status_code == 200
    guard = rail(client, config={'rule': 'keywords', 'terms': ['x'], 'action': 'redact'})
    profile = gateway(client, [service['id']], guardrails=[{'guardrail_id': guard['id'], 'stages': ['input']}],
                      limits=[{'metric': 'tokens', 'limit': 75000}] if limit == 'tokens' else [])
    _, auth = bound_key(client, profile)
    mock = swap_upstream(app, lambda _: httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}]}))
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(lambda _: invoke(client, service['name'], auth, content='x' * 5000, max_tokens=16).status_code, range(2)))
        assert sorted(statuses) == [200, expected]
        assert app.state.store.one('SELECT COUNT(*) AS n FROM attempts')['n'] == 1
        assert app.state.store.one('SELECT SUM(tokens) AS n FROM gateway_requests')['n'] < 75000
        assert app.state.store.one('SELECT COUNT(*) AS n FROM events')['n'] == 2
    finally:
        asyncio.run(mock.aclose())


@pytest.mark.parametrize('operation', ['chat', 'embeddings'])
@pytest.mark.parametrize('limit,expected', [('tokens', 429), ('budget', 402), ('input', 422)])
def test_expanding_redaction_must_fit_reserved_capacity(workspace, operation, limit, expected):
    client, app, _ = workspace
    service = capable(client)
    if limit == 'budget':
        assert client.patch('/api/v1/projects/default', json={'name': 'Default', 'monthly_budget': 0.02}).status_code == 200
    guard = rail(client, config={'rule': 'keywords', 'terms': ['x'], 'action': 'redact'})
    profile = gateway(client, [service['id']], guardrails=[{'guardrail_id': guard['id'], 'stages': ['input']}],
                      limits=[{'metric': 'tokens', 'limit': 10000}] if limit == 'tokens' else [],
                      max_input_chars=10000 if limit == 'input' else 200000)
    _, auth = bound_key(client, profile)
    sent = []

    def upstream(request):
        sent.append(request)
        if operation == 'embeddings':
            return httpx.Response(200, json={'data': [{'index': 0, 'embedding': [0.1]}]})
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}]})

    mock = swap_upstream(app, upstream)
    try:
        result = (invoke(client, service['name'], auth, content='x' * 5000, max_tokens=16) if operation == 'chat' else
                  client.post('/v1/embeddings', headers=auth, json={'model': service['name'], 'input': 'x' * 5000}))
        assert result.status_code == expected, result.text
        assert not sent
        assert app.state.store.one('SELECT COUNT(*) AS n FROM attempts')['n'] == 0
        assert app.state.store.one('SELECT reserved_micros FROM events')['reserved_micros'] == 0
        assert app.state.store.one('SELECT COUNT(*) AS n FROM events')['n'] == 1
    finally:
        asyncio.run(mock.aclose())


def test_python_sdk_closes_failed_http_response(monkeypatch):
    source = io.BytesIO(b'{"detail":"quota exceeded"}')
    error = HTTPError('https://gateway.test', 429, 'limit', {}, source)
    client = Pliqra('https://gateway.test', 'synthetic-key')

    def reject(*args, **kwargs):
        raise error

    monkeypatch.setattr(client.opener, 'open', reject)
    with pytest.raises(PliqraError) as caught:
        client.request('/protocol/v1/services')
    assert caught.value.status == 429
    assert source.closed


@pytest.mark.parametrize('ending', ['\n', '\r\n', '\r'])
def test_python_sdk_reads_complete_sse_events(monkeypatch, ending):
    raw = '\ufeff: keepalive' + ending + 'data: {"choices":' + ending + 'data: [{"delta":{"content":"hello 🌍"}}]}' + ending * 2 + 'data: [DONE]' + ending * 2
    client = Pliqra('https://gateway.test', 'synthetic-key')
    monkeypatch.setattr(client, 'request', lambda *args: io.BytesIO(raw.encode()))
    bound = Connection(client, {'service': 'test', 'invoke': '/invoke'})
    assert list(bound.stream([]))[0]['choices'][0]['delta']['content'] == 'hello 🌍'


@pytest.mark.parametrize('ending', ['', '\n', '\r\n', '\r'])
def test_python_sdk_does_not_dispatch_unterminated_terminal_event(monkeypatch, ending):
    client = Pliqra('https://gateway.test', 'synthetic-key')
    monkeypatch.setattr(client, 'request', lambda *args: io.BytesIO(('data: [DONE]' + ending).encode()))
    with pytest.raises(PliqraError):
        list(Connection(client, {'service': 'test', 'invoke': '/invoke'}).stream([]))


@pytest.mark.parametrize('ending', ['\n', '\r\n', '\r'])
def test_gateway_reads_standard_sse_framing(workspace, ending):
    client, app, _ = workspace
    service = capable(client)
    raw = '\ufeff: heartbeat' + ending + 'data: {"choices":' + ending + 'data: [{"delta":{"content":"hello 🌍"}}]}' + ending * 2 + 'data: [DONE]' + ending * 2
    mock = swap_upstream(app, lambda _: httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=Fragments(raw)))
    try:
        response = invoke(client, service['name'], stream=True)
        assert 'hello 🌍' in response.text and 'data: [DONE]' in response.text
        assert app.state.store.one('SELECT status FROM events')['status'] == 'success'
        assert ': heartbeat' in response.text
    finally:
        asyncio.run(mock.aclose())


@pytest.mark.parametrize('ending', ['', '\n', '\r\n', '\r'])
def test_gateway_does_not_accept_an_unterminated_terminal_event(workspace, ending):
    client, app, _ = workspace
    service = capable(client)
    mock = swap_upstream(app, lambda _: httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=Fragments('data: [DONE]' + ending)))
    try:
        response = invoke(client, service['name'], stream=True)
        assert 'incomplete_stream' in response.text and 'data: [DONE]' not in response.text
        assert app.state.store.one('SELECT status FROM events')['status'] == 'incomplete'
    finally:
        asyncio.run(mock.aclose())


@pytest.mark.parametrize('operation', ['chat', 'embeddings'])
@pytest.mark.parametrize('boundary', ['guardrail', 'secret'])
def test_fenced_replica_cannot_start_provider_io_after_await(workspace, monkeypatch, operation, boundary):
    client, app, _ = workspace
    service = capable(client)
    guard = rail(client, config={'rule': 'keywords', 'terms': ['forbidden']})
    profile = gateway(client, [service['id']], guardrails=[{'guardrail_id': guard['id'], 'stages': ['input']}])
    _, auth = bound_key(client, profile)
    engine = app.state.gateway
    if boundary == 'guardrail':
        original = engine.policies.check

        async def check(*args, **kwargs):
            value = await original(*args, **kwargs)
            engine.cluster.fence()
            return value

        monkeypatch.setattr(engine.policies, 'check', check)
    else:
        original = engine.secrets.resolve

        async def resolve(*args, **kwargs):
            value = await original(*args, **kwargs)
            engine.cluster.fence()
            return value

        monkeypatch.setattr(engine.secrets, 'resolve', resolve)
    sent = []

    def upstream(request):
        sent.append(request)
        if operation == 'embeddings':
            return httpx.Response(200, json={'data': [{'index': 0, 'embedding': [0.1]}]})
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}]})

    mock = swap_upstream(app, upstream)
    try:
        result = (invoke(client, service['name'], auth) if operation == 'chat' else
                  client.post('/v1/embeddings', headers=auth, json={'model': service['name'], 'input': 'hello'}))
        assert result.status_code == 503
        assert not sent, 'A fenced process started new provider work'
        assert app.state.store.one('SELECT COUNT(*) AS n FROM attempts')['n'] == 0
        assert app.state.store.one('SELECT COUNT(*) AS n FROM events')['n'] == 1
    finally:
        asyncio.run(mock.aclose())
