"""Exercise the installed CLI entry point against a real loopback HTTP server."""
import json
import os
import socket
import subprocess
import sys
import threading
import time

import httpx
import uvicorn

from pliqra.main import create_app


def test_policy_cli_exports_plans_applies_and_checks_drift(tmp_path):
    app = create_app(tmp_path/'workspace')
    app.state.upstream_transport = httpx.MockTransport(lambda r: httpx.Response(200,json={'data':[{'id':'model'}]}))
    server = uvicorn.Server(uvicorn.Config(app,log_level='error',proxy_headers=False))
    sock = socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    thread = threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True);thread.start()
    try:
        deadline = time.monotonic()+10
        while not server.started and time.monotonic()<deadline:time.sleep(.01)
        assert server.started
        url = f'http://127.0.0.1:{port}'
        with httpx.Client(base_url=url,trust_env=False) as client:
            response = client.post('/api/v1/setup',json={'name':'Owner','email':'owner@example.test','password':'long-local-password'})
            client.headers['X-Pliqra-CSRF'] = response.json()['csrf']
            result = client.post('/api/v1/connections',json={'name':'Model','endpoint':'https://model.test'}).json()
            service = result['services'][0]['id']
            created = client.post('/api/v1/gateways',json={'name':'Git gateway','slug':'git','config':{'services':[service]}})
            assert created.status_code == 201
        environment = {**os.environ,'PLIQRA_ADMIN_EMAIL':'owner@example.test','PLIQRA_ADMIN_PASSWORD':'long-local-password'}
        def cli(*args):
            return subprocess.run([sys.executable,'-m','pliqra','policy',*args,'--url',url],env=environment,capture_output=True,text=True,timeout=15)
        exported = cli('export','--slug','git')
        assert exported.returncode == 0, exported.stderr
        document = json.loads(exported.stdout)
        path = tmp_path/'policies.json';path.write_text(json.dumps(document))
        unchanged = cli('plan',str(path),'--check')
        assert unchanged.returncode == 0 and json.loads(unchanged.stdout)['has_changes'] is False
        document['gateways'][0]['config']['max_concurrency'] = 12
        path.write_text(json.dumps(document));planfile=tmp_path/'plan.json'
        changed = cli('plan',str(path),'--output',str(planfile),'--check','--revision','a'*40)
        assert changed.returncode == 2, changed.stderr
        assert planfile.stat().st_mode & 0o777 == 0o600
        applied = cli('apply',str(planfile))
        assert applied.returncode == 0 and json.loads(applied.stdout)['applied']
        assert cli('apply',str(planfile)).returncode == 1
        assert cli('plan',str(path),'--check').returncode == 0
        assert cli('plan',str(path),'--output',str(planfile)).returncode == 1
        assert 'long-local-password' not in exported.stdout+exported.stderr+changed.stdout+changed.stderr
    finally:
        server.should_exit=True;thread.join(timeout=10);sock.close()
        assert not thread.is_alive()
