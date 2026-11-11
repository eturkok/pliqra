"""Exercise real image upgrades and backup recovery in exclusively owned disposable storage."""
import argparse
import base64
import json
from pathlib import Path
import secrets
import subprocess
import time
import uuid
import sys

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'sdk'))
from pliqra_client import Pliqra


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True).strip()


class Fixture:
    def __init__(self):
        self.prefix = 'pliqra-upgrade-' + uuid.uuid4().hex[:12]
        self.containers = []
        self.volumes = []
        self.network = False
        self.checks = []

    def mark(self, name):
        self.checks.append({'check': name, 'passed': True})

    def volume(self, suffix):
        name = self.prefix + '-' + suffix
        docker('volume', 'create', '--label', 'pliqra.disposable-upgrade=' + self.prefix, name)
        self.volumes.append(name)
        return name

    def stop(self):
        if self.containers and self.containers[-1].endswith('-gateway'):
            docker('stop', '--time', '30', self.containers[-1])
            docker('rm', self.containers[-1])
            self.containers.pop()

    def gateway(self, image, volume):
        self.stop()
        name = self.prefix + '-gateway'
        self.containers.append(name)
        docker('run', '-d', '--name', name, '--network', self.prefix, '-v', volume + ':/data',
               '-p', '127.0.0.1::8080', image)
        port = json.loads(docker('inspect', name))[0]['NetworkSettings']['Ports']['8080/tcp'][0]['HostPort']
        client = httpx.Client(base_url='http://127.0.0.1:' + port, timeout=30, trust_env=False)
        deadline = time.monotonic() + 90
        while True:
            try:
                response = client.get('/readyz')
                if response.status_code == 200:
                    return client
            except httpx.HTTPError:
                pass
            if time.monotonic() >= deadline:
                client.close()
                raise RuntimeError('Owned upgrade gateway failed to become ready')
            time.sleep(.5)

    def close(self):
        errors = []
        for kind, args, names in [('containers', ['rm', '-f'], self.containers),
                                  ('volumes', ['volume', 'rm'], self.volumes),
                                  ('network', ['network', 'rm'], [self.prefix] if self.network else [])]:
            for name in reversed(names):
                assert name.startswith('pliqra-upgrade-')
                result = subprocess.run(['docker', *args, name], capture_output=True, text=True)
                if result.returncode:
                    errors.append(kind + ': ' + name)
        if errors:
            raise RuntimeError('Disposable upgrade cleanup failed: ' + ', '.join(errors))


def api(client, path, body=None, status=200):
    response = client.request('POST' if body is not None else 'GET', path, json=body)
    assert response.status_code == status, (path, response.status_code)
    return response


def authenticate(client, email, password, setup=False):
    value = api(client, '/api/v1/setup' if setup else '/api/v1/login',
                {'name': 'Synthetic release operator', 'email': email, 'password': password}).json()
    client.headers['X-Pliqra-CSRF'] = value['csrf']


def check(image, output):
    previous = json.loads((ROOT / 'release-policy.json').read_text())['previous_release']
    assert '@sha256:' in previous['image'], 'Previous release must be digest pinned'
    docker('pull', previous['image'])
    fixture = Fixture()
    report = {'synthetic': True, 'passed': False, 'previous': previous,
              'target_image': image, 'checks': fixture.checks}
    password = secrets.token_urlsafe(24)
    provider_secret = secrets.token_urlsafe(24)
    email = 'upgrade@example.test'
    clients = []
    try:
        docker('network', 'create', fixture.prefix)
        fixture.network = True
        model = fixture.prefix + '-model'
        fixture.containers.append(model)
        docker('run', '-d', '--name', model, '--network', fixture.prefix, '--network-alias', 'model',
               '-e', 'SYNTHETIC_EXPECTED_AUTH=Bearer ' + provider_secret,
               '-v', str(ROOT / 'tests/acceptance/provider.py') + ':/fixture/provider.py:ro',
               'python:3.13-slim', 'python', '/fixture/provider.py')
        volume = fixture.volume('workspace')
        client = fixture.gateway(previous['image'], volume)
        clients.append(client)
        assert api(client, '/healthz').json()['version'] == previous['version']
        authenticate(client, email, password, setup=True)
        project = api(client, '/api/v1/projects', {'name': 'Preserved project'}, 201).json()
        connection = api(client, '/api/v1/connections', {'name': 'Preserved encrypted endpoint',
                         'endpoint': 'http://model:8099/v1', 'provider': 'openai', 'api_key': provider_secret}, 201).json()
        service = connection['services'][0]
        compatibility_key=api(client,'/api/v1/keys',{'name':'New SDK on previous image','allowed_ports':[service['id']],'rpm':1000},201).json()['token']
        consumer=Pliqra(str(client.base_url),compatibility_key)
        assert consumer.metadata()['protocol'] == previous['protocol']
        connected=consumer.connect(service['name'],['chat','streaming'])
        messages=[{'role':'user','content':'New SDK on previous published image'}]
        assert connected.complete(messages,max_tokens=64)['choices']
        assert list(connected.stream(messages,max_tokens=64))
        fixture.mark('new_sdk_completes_and_streams_against_previous_published_image')
        profile = api(client, '/api/v1/gateways', {'name': 'Preserved policy', 'slug': 'upgrade-check',
                      'config': {'services': [service['id']], 'limits': [
                          {'metric': 'requests', 'scope': 'gateway', 'window_seconds': 86400, 'limit': 4}]}}, 201).json()
        key = api(client, '/api/v1/keys', {'name': 'Preserved key', 'project_id': project['id'],
                  'gateway_id': profile['id'], 'allowed_ports': [service['id']], 'rpm': 1000}, 201).json()['token']
        headers = {'Authorization': 'Bearer ' + key}
        body = {'model': service['name'], 'messages': [{'role': 'user', 'content': 'Synthetic upgrade check'}], 'max_tokens': 64}
        response = client.post('/v1/chat/completions', json=body, headers=headers)
        assert response.status_code == 200
        event_id = response.headers['X-Request-ID']
        binding = client.post('/protocol/v1/bindings', headers=headers, json={'service': service['name']})
        assert binding.status_code == 201
        invocation = binding.json()['invoke']
        archive = api(client, '/api/v1/backups', {'password': password}).content
        assert archive.startswith(b'PLQBACK01') and provider_secret.encode() not in archive
        fixture.mark('previous_digest_verified_initialized_and_encrypted_backup_created')
        client.close()

        client = fixture.gateway(image, volume)
        clients.append(client)
        target = api(client, '/healthz').json()['version']
        report['target_version'] = target
        authenticate(client, email, password)
        assert any(p['id'] == project['id'] for p in api(client, '/api/v1/projects').json())
        restored_connection = next(c for c in api(client, '/api/v1/connections').json() if c['id'] == connection['id'])
        assert restored_connection['has_secret']
        assert api(client, '/api/v1/events/' + event_id).json()['status'] == 'success'
        assert client.post('/v1/chat/completions', headers=headers, json=body).status_code == 200
        protocol = client.post(invocation, headers={**headers, 'Pliqra-Protocol': 'pliqra/1.0'}, json=body)
        assert protocol.status_code == 200 and protocol.headers['Pliqra-Protocol'] == 'pliqra/1.0'
        fixture.mark('pre_upgrade_binding_invokes_through_stable_protocol_without_rebinding')
        stable=client.post('/v1/chat/completions', headers={**headers,'X-Pliqra-Binding':binding.json()['id']}, json=body)
        assert stable.status_code==200 and stable.headers['X-Pliqra-Binding']==binding.json()['id']
        fixture.mark('pre_upgrade_binding_protects_existing_client_without_rebinding')
        assert client.post('/v1/chat/completions', headers=headers, json=body).status_code == 429
        fixture.mark('upgrade_preserves_accounts_models_encrypted_credentials_keys_events_bindings_and_quota_usage')
        api(client, '/api/v1/projects', {'name': 'After backup'}, 201)
        restore = {'password': 'incorrect-backup-password', 'archive': base64.b64encode(archive).decode(), 'confirmation': 'RESTORE'}
        api(client, '/api/v1/backups/restore', restore, 422)
        assert api(client, '/api/v1/session').json()['user'] is not None
        restore['password'] = password
        api(client, '/api/v1/backups/restore', restore)
        assert api(client, '/api/v1/session').json()['user'] is None
        authenticate(client, email, password)
        assert not any(p['name'] == 'After backup' for p in api(client, '/api/v1/projects').json())
        assert client.post('/v1/chat/completions', headers=headers, json=body).status_code == 200
        fixture.mark('new_image_restores_previous_backup_rejects_wrong_password_and_revokes_sessions')
        client.close()

        rollback = fixture.volume('rollback')
        client = fixture.gateway(previous['image'], rollback)
        clients.append(client)
        authenticate(client, email, password, setup=True)
        api(client, '/api/v1/backups/restore', restore)
        authenticate(client, email, password)
        assert api(client, '/healthz').json()['version'] == previous['version']
        assert api(client, '/api/v1/events/' + event_id).json()['status'] == 'success'
        assert client.post('/v1/chat/completions', headers=headers, json=body).status_code == 200
        fixture.mark('rollback_to_previous_image_restores_pre_upgrade_backup_in_a_separate_volume')
        report['passed'] = True
    except BaseException as error:
        report['failure'] = type(error).__name__
        raise
    finally:
        for client in clients:
            client.close()
        try:
            fixture.close()
            report['owned_containers_volumes_network_removed'] = True
        except BaseException:
            report['passed'] = False
            raise
        finally:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    check(args.image, args.output)
