"""Run the independent client profile over real local HTTP, with synthetic inference."""
import json
import socket
import threading
import time
from contextlib import contextmanager
from http.server import ThreadingHTTPServer

import uvicorn

from sdk.conformance import run
from sdk.pliqra_client import Pliqra
from tests.test_app import workspace,connect,key
from tests.mock_provider import Handler as Upstream
from examples.reference_server import Handler as Reference


@contextmanager
def http_server(handler):
    server=ThreadingHTTPServer(('127.0.0.1',0),handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield 'http://127.0.0.1:'+str(server.server_port)
    finally:server.shutdown();server.server_close();thread.join(3)


@contextmanager
def gateway_server(app):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(app,lifespan='off',log_level='error',timeout_graceful_shutdown=2))
        thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
        deadline=time.monotonic()+5
        while not server.started and thread.is_alive() and time.monotonic()<deadline:time.sleep(.01)
        assert server.started,'HTTP gateway did not start'
        try:yield 'http://127.0.0.1:'+str(port)
        finally:server.should_exit=True;thread.join(5)


def test_gateway_core_profile_over_http(workspace):
    client,app,_=workspace;service=connect(client)['services'][0]
    token=key(client,rpm=100,allowed_ports=[service['id']])['token']
    with gateway_server(app) as url:
        report=run(Pliqra(url,token),service['name'],invoke=True)
    assert report['passed'],json.dumps(report)
    assert len(report['results'])==13
    assert report['protocol']=='pliqra/1.0'


def test_independent_reference_core_profile_over_http(monkeypatch):
    with http_server(Upstream) as upstream:
        monkeypatch.setenv('REFERENCE_UPSTREAM',upstream+'/v1')
        monkeypatch.setenv('REFERENCE_MODEL','synthetic')
        monkeypatch.setenv('REFERENCE_TOKEN','synthetic-reference-key')
        with http_server(Reference) as url:
            report=run(Pliqra(url,'synthetic-reference-key'),'reference-chat',invoke=True)
    assert report['passed'],json.dumps(report)
    assert len(report['results'])==13
    assert report['protocol']=='pliqra/1.0'


def test_release_smoke_waits_for_connection_reset_during_startup(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from pliqra.main import create_app
    from pliqra import __version__
    from scripts import smoke_release

    original = smoke_release.urlopen
    attempts = []

    def starting_container(url, **kwargs):
        attempts.append(url)
        if len(attempts) == 1:
            raise ConnectionResetError('Container listener is not ready yet')
        return original(url, **kwargs)

    monkeypatch.setattr(smoke_release, 'urlopen', starting_container)
    app = create_app(tmp_path)
    with TestClient(app), gateway_server(app) as url:
        smoke_release.smoke(url, __version__)
    assert attempts[:2] == [url + '/healthz', url + '/healthz']
