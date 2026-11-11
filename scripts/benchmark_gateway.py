"""Compare one synthetic model directly, through a TLS relay, and through Pliqra.

Uses the owned acceptance lab with verified TLS and shared PostgreSQL. Ordinary
latency samples are uninstrumented. --profile runs a separate diagnostic pass.
No model credentials, prompts, sessions or fixture keys enter the public report.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, UTC
import json
from pathlib import Path
import statistics
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.customer_acceptance import Lab


class BenchmarkLab(Lab):
    def __init__(self, directory, image, profile=False, model_nodelay=False):
        super().__init__(directory, image)
        (self.directory / 'Caddyfile').write_text('https://:8080 {\n tls /fixture/gateway.crt /fixture/gateway.key\n reverse_proxy model-west:8099\n}\n')
        services = {'direct-tls': {'image': 'caddy:2-alpine', 'ports': ['127.0.0.1:18097:8080'],
                    'volumes': [str(self.directory / 'Caddyfile') + ':/etc/caddy/Caddyfile:ro',
                                str(self.directory / 'pki') + ':/fixture:ro']}}
        if model_nodelay:
            services.update({name: {'environment': {'SYNTHETIC_TCP_NODELAY': '1'}}
                             for name in ('model-west', 'model-east')})
        if profile:
            for name in ('gateway-west', 'gateway-east'):
                services[name] = {'environment': {'PYTHONPATH': '/diagnostic:/app'},
                    'volumes': [str(ROOT / 'tests/performance/profile_gateway.py') + ':/diagnostic/profile_gateway.py:ro'],
                    'command': ['python', '-m', 'uvicorn', 'profile_gateway:create_app', '--factory',
                                '--host', '0.0.0.0', '--port', '8080', '--no-access-log', '--no-proxy-headers',
                                '--ssl-certfile', '/fixture/gateway.crt', '--ssl-keyfile', '/fixture/gateway.key']}
        self.override = self.directory / 'benchmark.compose.json'
        self.override.write_text(json.dumps({'services': services}))

    def compose(self, *args, **kwargs):
        return super().compose('-f', str(self.override), *args, **kwargs)


def summary(values):
    ordered = sorted(values)
    return {name: round(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * q))], 3)
            for name, q in (('p50_ms', .5), ('p95_ms', .95), ('p99_ms', .99))}


def measure(client, token, models, count, concurrency, stream=False):
    def request(index):
        headers = {'Authorization': 'Bearer ' + token} if token else {}
        body = {'model': models[index % len(models)], 'messages': [{'role': 'user', 'content': 'Synthetic customer request'}],
                'max_tokens': 64, 'stream': stream}
        started = time.perf_counter()
        first = None
        with client.stream('POST', '/v1/chat/completions', headers=headers, json=body) as response:
            response.raise_for_status()
            if stream:
                complete = False
                for line in response.iter_lines():
                    if not line.startswith('data:'): continue
                    data = line[5:].strip()
                    if data == '[DONE]': complete = True; continue
                    packet = json.loads(data)
                    if first is None and any(c.get('delta', {}).get('content') for c in packet.get('choices', [])):
                        first = (time.perf_counter() - started) * 1000
                assert complete and first is not None, 'Incomplete synthetic stream'
            else:
                response.read()
                packet = response.json()
                assert packet['choices'][0]['message']['content'].startswith('Synthetic customer acceptance')
            diagnostic = {key: float(response.headers['X-Benchmark-DB-' + key])
                          for key in ('calls', 'ms', 'clock', 'lock', 'settings', 'other')
                          if 'X-Benchmark-DB-' + key in response.headers}
        return {'elapsed_ms': (time.perf_counter() - started) * 1000, 'first_content_ms': first, 'sql': diagnostic}

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        samples = list(pool.map(request, range(count)))
    elapsed = time.perf_counter() - started
    result = {'requests': count, 'concurrency': concurrency, 'duration_seconds': round(elapsed, 3),
              'requests_per_second': round(count / elapsed, 3), 'completion': summary([s['elapsed_ms'] for s in samples])}
    if stream: result['first_content'] = summary([s['first_content_ms'] for s in samples])
    if samples[0]['sql']:
        result['sql_mean_per_request'] = {key: round(statistics.mean(s['sql'][key] for s in samples), 3)
                                          for key in samples[0]['sql']}
    return result


def run(args):
    lab = BenchmarkLab(args.state, args.image, args.profile, args.model_nodelay)
    report = {'passed': False, 'checked_at': datetime.now(UTC).isoformat(), 'diagnostic_only': args.profile,
              'environment': lab.report['environment'], 'runs': [],
              'method': {'model_delay_ms': 5, 'warmup_requests_per_path': 16, 'rounds': args.rounds,
                         'model_tcp_nodelay': args.model_nodelay,
                         'order': 'alternates each round', 'connection_policy': 'reused httpx clients; default HTTP/1.1',
                         'sql_diagnostics': 'explicit execute calls only; implicit BEGIN/COMMIT excluded; JSON headers only',
                         'paths': {'direct_http': 'client -> same HTTP model',
                                   'direct_tls': 'client -> verified-TLS Caddy relay -> same HTTP model',
                                   'pliqra': 'client -> verified-TLS gateway -> same HTTP model; shared PostgreSQL with verified TLS'},
                         'boundary': 'Synthetic same-host comparison; quantile differences are not per-request added latency or production capacity.'}}
    clients = []
    try:
        lab.setup()
        direct = httpx.Client(base_url='http://127.0.0.1:18095', timeout=20, trust_env=False)
        relay = httpx.Client(base_url='https://127.0.0.1:18097', verify=lab.verify, timeout=20, trust_env=False)
        clients.extend([direct, relay])
        # Use the same west model, rather than changing model/site between paths.
        deployment = next(d for d in lab.west.api('/api/v1/connections') if d['name'] == 'Synthetic west')['models'][0]
        service = lab.west.api('/api/v1/ports', {'name': 'benchmark-west', 'deployments': [deployment['id']],
                    'requirements': ['chat'], 'placement': {'routing': 'local_only'}}, status=201)
        lab.west.api('/api/v1/projects/default', {'name': 'Measured project', 'monthly_budget': 10000}, method='PATCH')
        policy = lab.west.api('/api/v1/gateways', {'name': 'Measured policy', 'slug': 'benchmark',
            'config': {'services': [service['id']], 'limits': [
                {'metric': 'requests', 'scope': 'gateway', 'window_seconds': 60, 'limit': 100000},
                {'metric': 'tokens', 'scope': 'gateway', 'window_seconds': 60, 'limit': 10000000}]}}, status=201)
        key = lab.west.api('/api/v1/keys', {'name': 'Measured governed client', 'project_id': 'default',
                          'gateway_id': policy['id'], 'allowed_ports': [service['id']], 'rpm': 100000}, status=201)['token']
        report['method']['governance'] = 'scoped key, request/token gateway quotas, project budget and concurrency; caching disabled'
        paths = [('direct_http', direct, '', ['synthetic-chat']), ('direct_tls', relay, '', ['synthetic-chat']),
                 ('pliqra', lab.west.client, key, [service['id']])]
        for name, client, token, models in paths:
            measure(client, token, models, 16, 1)
        for repeat in range(args.rounds):
            for concurrency in args.concurrency:
                for mode in args.modes:
                    for name, client, token, models in (paths if repeat % 2 == 0 else reversed(paths)):
                        measure(client, token, models, 16, concurrency, mode == 'stream')
                        result = {'round': repeat + 1, 'path': name, 'mode': mode,
                                  **measure(client, token, models, args.requests, concurrency, mode == 'stream')}
                        report['runs'].append(result)
                        print(json.dumps(result), flush=True)
        lab.routing_and_quotas()
        report['safety_checks'] = lab.report['checks']
        report['passed'] = True
    finally:
        for client in clients: client.close()
        lab.close(args.keep)
        report['owned_lab_removed'] = not args.keep
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--state', type=Path, required=True, help='New owned lab directory; never use a real workspace')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--requests', type=int, default=96)
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--concurrency', type=lambda v: [int(n) for n in v.split(',')], default=[1, 8])
    parser.add_argument('--modes', type=lambda v: v.split(','), default=['json', 'stream'])
    parser.add_argument('--profile', action='store_true', help='Separate SQL diagnostic pass; never compare its timings with ordinary runs')
    parser.add_argument('--model-nodelay', action='store_true', help='Disable model-fixture Nagle buffering as a separately labelled transport control')
    parser.add_argument('--keep', action='store_true', help='Keep only the owned disposable lab for investigation')
    args = parser.parse_args()
    if not (16 <= args.requests <= 4000 and 1 <= args.rounds <= 5 and args.concurrency
            and all(1 <= n <= 16 for n in args.concurrency) and args.modes
            and set(args.modes) <= {'json', 'stream'}): parser.error('Benchmark bounds exceeded')
    run(args)
