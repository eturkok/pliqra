"""Install the packaged chart only in an explicitly owned disposable kind cluster."""
import argparse
import base64
from contextlib import contextmanager
import http.cookiejar
import json
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = '''from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def send(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def do_GET(self):
        self.send(200, {'data': [{'id': 'chart-fixture'}]})
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.headers.get('Authorization') != 'Bearer fixture-secret':
            return self.send(401, {'error': 'Synthetic credential mismatch'})
        self.send(200, {'id': 'fixture', 'model': body['model'], 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'Chart acceptance passed'}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5}})
ThreadingHTTPServer(('0.0.0.0', 8090), Handler).serve_forever()
'''
RECOVERY_DRILL = '''import base64, json, os, tempfile, uuid
from pathlib import Path
from fastapi import HTTPException
from pliqra.db import SQLiteStore
from pliqra.operations import restore_bytes
from pliqra.security import Vault
data = json.load(__import__('sys').stdin)
archive = base64.b64decode(data['archive'], validate=True)
schema = 'drill_' + uuid.uuid4().hex
store = None
try:
    with tempfile.TemporaryDirectory(prefix='pliqra-restore-drill-') as directory:
        directory = Path(directory)
        if data['profile'] == 'postgresql':
            from pliqra.postgres import PostgresStore
            store = PostgresStore(directory, os.environ['PLIQRA_DATABASE_URL'], schema)
        else:
            store = SQLiteStore(directory)
        vault = Vault(directory, store)
        try:
            restore_bytes(store, vault, archive, 'deliberately-wrong-password')
        except HTTPException as error:
            assert error.status_code == 422
        else:
            raise AssertionError('Incorrect archive password was accepted')
        assert store.one('SELECT COUNT(*) AS n FROM users')['n'] == 0
        restore_bytes(store, vault, archive, data['password'])
        assert store.one('SELECT COUNT(*) AS n FROM sessions')['n'] == 0
        assert store.one("SELECT id FROM users WHERE role='admin' AND active=1")
        assert store.one('SELECT id FROM bindings WHERE id=?', (data['binding'],))
        credential = store.one('SELECT secret FROM connections WHERE id=?', (data['connection'],))
        assert vault.decrypt(credential['secret']) == 'fixture-secret'
        print('Isolated restore verified: admin, binding, encrypted credential, revoked sessions; wrong password rejected.')
finally:
    if store is not None:
        store.close()
    if data['profile'] == 'postgresql':
        import psycopg
        from psycopg import sql
        with psycopg.connect(os.environ['PLIQRA_DATABASE_URL'], autocommit=True) as db:
            db.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))
'''


def command(args, *, payload=None):
    result = subprocess.run(args, input=payload, text=True, capture_output=True, timeout=420)
    if result.returncode:
        # Request bodies and Secret contents are never included in diagnostics.
        raise RuntimeError('Command failed: ' + ' '.join(args[:5]) + '\n' + result.stderr[-4000:])
    return result.stdout


class Lab:
    def __init__(self, context, chart, profile):
        self.context, self.chart, self.profile = context, chart, profile
        self.namespace = 'pliqra-chart-' + profile + '-' + uuid.uuid4().hex[:8]
        self.checks = []
        self.values = {} if profile == 'sqlite' else {
            'storage': {'mode': 'postgresql'}, 'replicaCount': 2,
            'database': {'existingSecret': 'backend', 'tls': {'existingSecret': 'database-ca'}},
            'encryption': {'existingSecret': 'backend'},
            'topology': {'multiDC': True, 'site': 'ci-west', 'region': 'ci'},
        }

    def kube(self, *args, payload=None):
        return command(['kubectl', '--context', self.context, '-n', self.namespace, *args], payload=payload)

    def helm(self, *args):
        return command(['helm', *args, '--kube-context', self.context, '-n', self.namespace])

    def apply(self, *objects):
        self.kube('apply', '-f', '-', payload=json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': list(objects)}))

    def mark(self, name):
        self.checks.append({'check': name, 'passed': True})
        print(self.profile + ': ' + name, flush=True)

    def pods(self):
        data = json.loads(self.kube('get', 'pods', '-l', 'app.kubernetes.io/instance=pliqra', '-o', 'json'))
        return sorted(p['metadata']['name'] for p in data['items']
                      if not p['metadata'].get('deletionTimestamp') and p['status'].get('phase') == 'Running'
                      and all(c.get('ready') for c in p['status'].get('containerStatuses', [])))

    def ready(self, count):
        self.kube('rollout', 'status', 'deployment/pliqra', '--timeout=240s')
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            found = self.pods()
            if len(found) == count:
                return found
            time.sleep(1)
        raise RuntimeError('Expected ready replica count was not reached')

    @contextmanager
    def forward(self, pod):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        with tempfile.TemporaryFile(mode='w+') as log:
            process = subprocess.Popen(['kubectl', '--context', self.context, '-n', self.namespace,
                                        'port-forward', '--address', '127.0.0.1', 'pod/' + pod,
                                        f'{port}:8080'], stdout=log, stderr=log)
            base = f'http://127.0.0.1:{port}'
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError('Owned port-forward terminated')
                    try:
                        with urllib.request.urlopen(base + '/readyz', timeout=2) as response:
                            if response.status == 200:
                                break
                    except (OSError, urllib.error.URLError):
                        time.sleep(.3)
                else:
                    raise RuntimeError('Owned port-forward did not become ready')
                yield base
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

    def fixtures(self, image):
        self.apply({'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'fixture-code'}, 'data': {'provider.py': FIXTURE}},
                   {'apiVersion': 'v1', 'kind': 'Service', 'metadata': {'name': 'fixture'},
                    'spec': {'selector': {'fixture': 'model'}, 'ports': [{'port': 8090, 'targetPort': 8090}]}},
                   {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': {'name': 'fixture'},
                    'spec': {'replicas': 1, 'selector': {'matchLabels': {'fixture': 'model'}}, 'template': {
                        'metadata': {'labels': {'fixture': 'model'}}, 'spec': {
                            'automountServiceAccountToken': False,
                            'containers': [{'name': 'fixture', 'image': image, 'command': ['python', '/fixture/provider.py'],
                                            'volumeMounts': [{'name': 'code', 'mountPath': '/fixture', 'readOnly': True}],
                                            'resources': {'requests': {'cpu': '10m', 'memory': '32Mi'}, 'limits': {'memory': '128Mi'}}}],
                            'volumes': [{'name': 'code', 'configMap': {'name': 'fixture-code'}}]}}}})
        self.kube('rollout', 'status', 'deployment/fixture', '--timeout=180s')
        if self.profile == 'sqlite':
            return
        host = 'postgres.' + self.namespace + '.svc'
        with tempfile.TemporaryDirectory(prefix='pliqra-chart-tls-') as directory:
            directory = Path(directory)
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                            '-subj', '/CN=' + host, '-addext', 'subjectAltName=DNS:' + host,
                            '-keyout', str(directory / 'key'), '-out', str(directory / 'cert')],
                           check=True, capture_output=True)
            cert, key = (directory / 'cert').read_text(), (directory / 'key').read_text()
        password = secrets.token_urlsafe(24)
        dsn = f'host={host} port=5432 dbname=pliqra user=pliqra password={password} sslmode=verify-full sslrootcert=/run/pliqra/database/ca.crt'
        self.apply({'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'backend'},
                    'stringData': {'database-url': dsn, 'encryption-key': base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()}},
                   {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'database-ca'}, 'stringData': {'ca.crt': cert}},
                   {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'postgres-tls'}, 'stringData': {'tls.crt': cert, 'tls.key': key}},
                   {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'postgres-auth'}, 'stringData': {'password': password}},
                   {'apiVersion': 'v1', 'kind': 'Service', 'metadata': {'name': 'postgres'},
                    'spec': {'selector': {'fixture': 'postgres'}, 'ports': [{'port': 5432, 'targetPort': 5432}]}},
                   {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': {'name': 'postgres'},
                    'spec': {'replicas': 1, 'selector': {'matchLabels': {'fixture': 'postgres'}}, 'template': {
                        'metadata': {'labels': {'fixture': 'postgres'}}, 'spec': {
                            'securityContext': {'runAsUser': 999, 'runAsGroup': 999, 'fsGroup': 999},
                            'containers': [{'name': 'postgres', 'image': 'postgres:18-bookworm',
                                'args': ['-c', 'ssl=on', '-c', 'ssl_cert_file=/tls/tls.crt', '-c', 'ssl_key_file=/tls/tls.key'],
                                'env': [{'name': 'POSTGRES_DB', 'value': 'pliqra'}, {'name': 'POSTGRES_USER', 'value': 'pliqra'},
                                        {'name': 'PGDATA', 'value': '/var/lib/postgresql/data/pgdata'},
                                        {'name': 'POSTGRES_PASSWORD', 'valueFrom': {'secretKeyRef': {'name': 'postgres-auth', 'key': 'password'}}}],
                                'readinessProbe': {'exec': {'command': ['pg_isready', '-U', 'pliqra', '-d', 'pliqra']}, 'periodSeconds': 3},
                                'resources': {'requests': {'cpu': '50m', 'memory': '128Mi'}, 'limits': {'memory': '512Mi'}},
                                'volumeMounts': [{'name': 'pgdata', 'mountPath': '/var/lib/postgresql/data'}, {'name': 'tls', 'mountPath': '/tls', 'readOnly': True}]}],
                            'volumes': [{'name': 'pgdata', 'emptyDir': {}}, {'name': 'tls', 'secret': {'secretName': 'postgres-tls', 'defaultMode': 0o440}}]}}}})
        self.kube('rollout', 'status', 'deployment/postgres', '--timeout=240s')

    def exercise(self, image):
        self.kube('create', 'namespace', self.namespace)
        try:
            self.fixtures(image)
            with tempfile.TemporaryDirectory(prefix='pliqra-chart-values-') as directory:
                values = Path(directory) / 'values.json'
                initial = self.values if self.profile == 'postgresql' else {'image': {'digest': '', 'tag': '2.1.0-rc.4'}}
                values.write_text(json.dumps(initial))
                self.helm('install', 'pliqra', str(self.chart), '-f', str(values), '--wait', '--timeout', '5m')
                count = 1 if self.profile == 'sqlite' else 2
                pods = self.ready(count)
                self.helm('test', 'pliqra', '--timeout', '90s')
                self.mark('packaged chart installs and helm test passes')
                cookies = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
                csrf = None

                def request(base, path, body=None, token=None, expected=200):
                    headers = {'Content-Type': 'application/json'}
                    if csrf:
                        headers['X-Pliqra-CSRF'] = csrf
                    if token:
                        headers['Authorization'] = 'Bearer ' + token
                    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), headers=headers)
                    opener = urllib.request.build_opener() if token else cookies
                    try:
                        response = opener.open(req, timeout=20)
                    except urllib.error.HTTPError as error:
                        response = error
                    with response:
                        payload = json.load(response)
                        assert response.status == expected, f'{path}: expected {expected}, got {response.status}'
                        return payload

                with self.forward(pods[0]) as base:
                    initial_version = '2.1.0-rc.4' if self.profile == 'sqlite' else '2.1.0'
                    assert request(base, '/readyz')['version'] == initial_version
                    setup = request(base, '/api/v1/setup', {'name': 'Chart Owner', 'email': 'chart@example.test', 'password': secrets.token_urlsafe(24)})
                    csrf = setup['csrf']
                    connection = request(base, '/api/v1/connections', {'name': 'Chart fixture', 'endpoint': 'http://fixture:8090/v1', 'api_key': 'fixture-secret'}, expected=201)
                    service = connection['services'][0]
                    binding = request(base, '/protocol/v1/bindings', {'service': service['name']}, expected=201)
                    body = {'model': service['name'], 'messages': [{'role': 'user', 'content': 'Synthetic chart acceptance'}]}
                    result = request(base, binding['invoke'], body)
                    assert result['choices'][0]['message']['content'] == 'Chart acceptance passed'
                    key = request(base, '/api/v1/keys', {'name': 'Shared chart limit', 'rpm': 2, 'allowed_ports': [service['id']]}, expected=201)
                self.mark('private onboarding, encrypted provider credential and bound inference')
                if self.profile == 'sqlite':
                    unsafe = Path(directory) / 'unsafe.json'
                    for override, message in [
                        ({'storage': {'mode': 'postgresql'}, 'database': {'existingSecret': 'backend'}, 'encryption': {'existingSecret': 'backend'}}, 'storage.mode cannot change'),
                        ({'persistence': {'existingClaim': 'different-volume'}}, 'data claim cannot change'),
                    ]:
                        unsafe.write_text(json.dumps(override))
                        try:
                            self.helm('upgrade', 'pliqra', str(self.chart), '-f', str(unsafe))
                        except RuntimeError as error:
                            assert message in str(error), str(error)
                        else:
                            raise AssertionError('Unsafe in-place storage change was accepted')
                    self.mark('live upgrade rejects storage-backend and SQLite volume replacement')
                for pod in pods:
                    with self.forward(pod) as base:
                        assert request(base, '/api/v1/session')['user']['role'] == 'admin'
                if self.profile == 'postgresql':
                    for pod in pods:
                        with self.forward(pod) as base:
                            request(base, '/v1/chat/completions', body, token=key['token'])
                    with self.forward(pods[0]) as base:
                        request(base, '/v1/chat/completions', body, token=key['token'], expected=429)
                    self.mark('verified-TLS shared PostgreSQL, cross-pod sessions and exact shared request quota')
                pvc_uid = json.loads(self.kube('get', 'pvc', 'pliqra', '-o', 'json'))['metadata']['uid'] if self.profile == 'sqlite' else None
                values.write_text(json.dumps(self.values))
                self.helm('upgrade', 'pliqra', str(self.chart), '-f', str(values), '--set-string', 'podAnnotations.acceptance=upgrade', '--wait', '--timeout', '5m')
                pods = self.ready(count)
                with self.forward(pods[0]) as base:
                    assert not request(base, '/api/v1/session')['setup_required']
                    assert request(base, '/readyz')['version'] == '2.1.0'
                    request(base, binding['invoke'], body)
                self.mark(('RC4-to-2.1.0 image' if self.profile == 'sqlite' else 'Recreate configuration') + ' upgrade preserves identity, bindings and encrypted provider access')
                with self.forward(pods[0]) as base:
                    password = secrets.token_urlsafe(24)
                    req = urllib.request.Request(base + '/api/v1/backups',
                        data=json.dumps({'password': password}).encode(),
                        headers={'Content-Type': 'application/json', 'X-Pliqra-CSRF': csrf})
                    with cookies.open(req, timeout=30) as response:
                        archive = response.read()
                    payload = {'profile': self.profile, 'password': password,
                               'archive': base64.b64encode(archive).decode(),
                               'binding': binding['id'], 'connection': connection['id']}
                    result = self.kube('exec', '-i', pods[0], '--', 'python', '-c', RECOVERY_DRILL,
                                       payload=json.dumps(payload))
                    assert 'Isolated restore verified' in result
                    # The drill uses another SQLite directory/PostgreSQL schema;
                    # source identity and inference must remain usable.
                    request(base, binding['invoke'], body)
                    monitoring = request(base, '/api/v1/monitoring-token', {})['token']
                    for token, expected in [(None, 401), (monitoring, 200)]:
                        headers = {'Authorization': 'Bearer ' + token} if token else {}
                        try:
                            response = urllib.request.urlopen(urllib.request.Request(base + '/metrics', headers=headers), timeout=10)
                        except urllib.error.HTTPError as error:
                            response = error
                        with response:
                            content = response.read()
                            assert response.status == expected
                            if token:
                                assert b'pliqra_unresolved_costs' in content
                self.mark('encrypted backup restores into an isolated target; wrong password rejected and source remains usable')
                self.apply({'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'monitoring'},
                            'stringData': {'token': monitoring}})
                before_revision = set(pods)
                monitored = {**self.values, 'config': {'revision': 'monitoring-1'},
                             'monitoring': {'enabled': True, 'existingSecret': 'monitoring', 'prometheusRule': {'enabled': True}},
                             'topologySpreadConstraints': [{'maxSkew': 1, 'topologyKey': 'kubernetes.io/hostname', 'whenUnsatisfiable': 'ScheduleAnyway'}]}
                values.write_text(json.dumps(monitored))
                self.helm('upgrade', 'pliqra', str(self.chart), '-f', str(values), '--wait', '--timeout', '5m')
                pods = self.ready(count)
                assert not before_revision.intersection(pods)
                monitor = json.loads(self.kube('get', 'servicemonitor', 'pliqra', '-o', 'json'))
                assert monitor['spec']['endpoints'][0]['authorization']['credentials']['name'] == 'monitoring'
                assert len(json.loads(self.kube('get', 'prometheusrule', 'pliqra', '-o', 'json'))['spec']['groups'][0]['rules']) == 3
                with self.forward(pods[0]) as base:
                    request(base, binding['invoke'], body)
                self.mark('authenticated metrics, monitoring CRDs, topology spread and config revision rollout preserve service state')
                self.helm('rollback', 'pliqra', '1', '--wait', '--timeout', '5m')
                pods = self.ready(count)
                with self.forward(pods[0]) as base:
                    assert request(base, '/readyz')['version'] == initial_version
                    request(base, binding['invoke'], body)
                self.mark(('same-schema image' if self.profile == 'sqlite' else 'same-image configuration') + ' Helm rollback preserves application state')
                if self.profile == 'sqlite':
                    assert json.loads(self.kube('get', 'pvc', 'pliqra', '-o', 'json'))['metadata']['uid'] == pvc_uid
                    self.helm('uninstall', 'pliqra', '--wait', '--timeout', '3m')
                    assert json.loads(self.kube('get', 'pvc', 'pliqra', '-o', 'json'))['metadata']['uid'] == pvc_uid
                    self.helm('install', 'pliqra', str(self.chart), '-f', str(values), '--wait', '--timeout', '5m')
                    with self.forward(self.ready(1)[0]) as base:
                        request(base, binding['invoke'], body)
                    self.mark('PVC retention and same-release reinstall preserve data and encryption key')
                else:
                    scaled = {**self.values, 'replicaCount': 3}
                    values.write_text(json.dumps(scaled))
                    self.helm('upgrade', 'pliqra', str(self.chart), '-f', str(values), '--wait', '--timeout', '5m')
                    for pod in self.ready(3):
                        with self.forward(pod) as base:
                            request(base, binding['invoke'], body)
                    self.mark('three PostgreSQL replicas retain the accepted contract and provider access')
        except BaseException:
            print(self.kube('get', 'pods', '-o', 'wide'), flush=True)
            # Pod events do not contain fixture Secret values or API payloads.
            print(self.kube('get', 'events', '--sort-by=.metadata.creationTimestamp'), flush=True)
            raise
        finally:
            self.kube('delete', 'namespace', self.namespace, '--wait=false')


def main():
    import yaml
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context', required=True)
    parser.add_argument('--chart', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not args.context.startswith('kind-pliqra-chart-'):
        parser.error('Only explicitly owned kind-pliqra-chart-* contexts are allowed')
    chart = args.chart.resolve()
    metadata = yaml.safe_load(command(['helm', 'show', 'chart', str(chart)]))
    values = yaml.safe_load(command(['helm', 'show', 'values', str(chart)]))
    image = values['image']['repository'] + '@' + values['image']['digest']
    report = {'passed': False, 'chart_version': metadata['version'], 'app_version': metadata['appVersion'],
              'image': image, 'context': args.context, 'profiles': [],
              'boundary': 'Disposable single-node kind clusters and synthetic providers; SQLite RC4-to-2.1.0 upgrade/rollback on schema 6, PostgreSQL configuration upgrades, isolated physical/logical recovery and monitoring CRD schemas/authentication. No Prometheus Operator reconciliation, zone failure, incompatible schema downgrade or model capacity certification.'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        for profile in ('sqlite', 'postgresql'):
            lab = Lab(args.context, chart, profile)
            report['profiles'].append({'name': profile, 'checks': lab.checks})
            lab.exercise(image)
        report['passed'] = True
    finally:
        args.output.write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    main()
