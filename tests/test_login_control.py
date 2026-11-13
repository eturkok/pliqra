from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from pliqra.db import Store
from pliqra.login_control import LoginControl
from pliqra.main import create_app
from pliqra.security import Vault
from tests.test_scale import replicas


def request(peer='127.0.0.1', forwarded=None):
    headers = [] if forwarded is None else [(b'x-forwarded-for', forwarded.encode())]
    return Request({'type':'http','client':(peer,12345),'headers':headers})


def test_shared_login_throttle_survives_restart_and_expires(tmp_path):
    store = Store(tmp_path)
    vault = Vault(tmp_path,store)
    control = LoginControl(store,vault)
    instant = store.clock()
    store.clock = lambda: instant
    for _ in range(15): control.check(request())
    with pytest.raises(HTTPException) as denied: control.check(request())
    assert denied.value.status_code == 429 and denied.value.headers['Retry-After'] == '300'
    assert all('127.0.0.1' not in row['bucket'] for row in store.all('SELECT * FROM auth_attempts'))
    store.close()
    store = Store(tmp_path)
    control = LoginControl(store,Vault(tmp_path,store))
    with pytest.raises(HTTPException): control.check(request())
    store.clock = lambda: instant + 300
    control.check(request())
    assert store.one('SELECT COUNT(*) AS n FROM auth_attempts')['n'] == 1
    store.close()


def test_trusted_proxy_chain_cannot_be_spoofed(tmp_path,monkeypatch):
    store = Store(tmp_path)
    vault = Vault(tmp_path,store)
    default = LoginControl(store,vault)
    assert default.address(request('198.51.100.4','203.0.113.7')) == '198.51.100.4'
    monkeypatch.setenv('PLIQRA_TRUSTED_PROXIES','10.0.0.0/8,2001:db8::/32')
    control = LoginControl(store,vault)
    assert control.address(request('10.0.0.4','192.0.2.99, 203.0.113.7, 10.1.1.1')) == '203.0.113.7'
    assert control.address(request('2001:db8::1','2001:4860::1')) == '2001:4860::1'
    with pytest.raises(HTTPException): control.address(request('10.0.0.4','invalid'))
    duplicate = Request({'type':'http','client':('10.0.0.4',1),'headers':[(b'x-forwarded-for',b'1.1.1.1')]*2})
    with pytest.raises(HTTPException): control.address(duplicate)
    # Spoofed headers cannot split one direct client's quota across fresh buckets.
    for i in range(15): default.check(request('198.51.100.4',f'192.0.2.{i}'))
    with pytest.raises(HTTPException): default.check(request('198.51.100.4','192.0.2.200'))
    store.close()


def test_invalid_proxy_configuration_releases_storage_lock(tmp_path,monkeypatch):
    monkeypatch.setenv('PLIQRA_TRUSTED_PROXIES','invalid-cidr')
    with pytest.raises(ValueError): create_app(tmp_path)
    monkeypatch.delenv('PLIQRA_TRUSTED_PROXIES')
    app = create_app(tmp_path)
    app.state.store.close()


def test_login_admission_is_atomic_across_real_postgres_replicas(replicas):
    a,b,first,second = replicas
    first.state.store.run('DELETE FROM auth_attempts')
    def attempt(index):
        control = (first if index%2 else second).state.login_control
        try:
            control.check(request('203.0.113.8'))
            return 200
        except HTTPException as error: return error.status_code
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(attempt,range(30)))
    assert results.count(200) == results.count(429) == 15
    assert first.state.store.one('SELECT COUNT(*) AS n FROM auth_attempts')['n'] == 15


def test_fast_replica_housekeeping_cannot_erase_live_login_buckets(tmp_path,monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from pliqra import operations
    app = create_app(tmp_path)
    store = app.state.store
    store.set_setting('runtime',{'health_checks':False})
    instant = store.clock();store.clock = lambda:instant
    for _ in range(15):app.state.login_control.check(request())
    app.state.gateway = SimpleNamespace(maintenance=False)
    ticks = 0
    async def advance(_):
        nonlocal ticks
        ticks += 1
        if ticks>6:raise asyncio.CancelledError()
    monkeypatch.setattr(operations.asyncio,'sleep',advance)
    monkeypatch.setattr(operations,'now',lambda:instant+3600)
    try:
        with pytest.raises(asyncio.CancelledError):asyncio.run(operations.maintain(app))
        assert store.one('SELECT COUNT(*) AS n FROM auth_attempts')['n'] == 15
        with pytest.raises(HTTPException):app.state.login_control.check(request())
    finally:store.close()
