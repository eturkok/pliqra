"""Small Git-friendly client: python -m pliqra policy plan|apply|export."""
import argparse
import getpass
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from .config import http_verify


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['plan', 'apply', 'export'])
    parser.add_argument('file', nargs='?', type=Path)
    parser.add_argument('--url', default='http://localhost:8088')
    parser.add_argument('--email', default=os.getenv('PLIQRA_ADMIN_EMAIL'))
    parser.add_argument('--output', type=Path)
    parser.add_argument('--revision', default='')
    parser.add_argument('--slug')
    parser.add_argument('--check', action='store_true', help='Exit 2 when plan detects changes')
    args = parser.parse_args(argv)
    url = args.url.rstrip('/')
    parts = urlsplit(url)
    if parts.username or parts.password or parts.query or parts.fragment or parts.scheme not in ('http', 'https'):
        parser.error('Use a plain HTTPS instance URL, without credentials, query or fragment')
    if parts.scheme != 'https' and parts.hostname not in ('localhost', '127.0.0.1', '::1'):
        parser.error('Remote administration requires verified HTTPS')
    if args.action != 'export' and not args.file or args.action == 'export' and not args.slug:
        parser.error('plan/apply need a JSON file; export needs --slug')
    if not args.email:
        parser.error('Set PLIQRA_ADMIN_EMAIL or --email')
    password = os.getenv('PLIQRA_ADMIN_PASSWORD') or getpass.getpass('Administrator password: ')
    with httpx.Client(base_url=url+'/', verify=http_verify(), trust_env=False, timeout=30, follow_redirects=False) as client:
        logged_in = False
        def api(path, body=None):
            response = client.request('POST' if body is not None else 'GET', path.lstrip('/'), json=body)
            if not response.is_success:
                raise ValueError('Policy request failed with HTTP ' + str(response.status_code) + '. Review the current configuration; apply is never retried automatically.')
            return response.json()
        try:
            login = api('/api/v1/login', {'email': args.email, 'password': password})
            logged_in = True
            client.headers['X-Pliqra-CSRF'] = login['csrf']
            if args.action == 'export':
                profiles = api('/api/v1/gateways')
                profile = next((p for p in profiles if p['slug'] == args.slug), None)
                if not profile: raise ValueError('Gateway address not found')
                item = api('/api/v1/gateways/' + profile['id'] + '/export')
                config = item['config']
                result = {'format': 'pliqra/policies/1', 'gateways': [{k: item[k] for k in ('name','slug','config')}],
                          'mapping': {'services': {i:i for i in config['services']},
                                      'guardrails': {r['guardrail_id']:r['guardrail_id'] for r in config['guardrails']},
                                      'projects': {config['project_id']:config['project_id']} if config['project_id'] else {},
                                      'teams': {i:i for i in config['team_ids']}}}
            else:
                if args.file.stat().st_size > 2000000: raise ValueError('Input exceeds 2 MB')
                document = json.loads(args.file.read_text())
                if args.action == 'plan':
                    if args.revision: document['source_revision'] = args.revision
                    result = api('/api/v1/policies/plan', document)
                else:
                    result = api('/api/v1/policies/apply', {'plan': document['plan'], 'approve': True})
            data = json.dumps(result, indent=2) + '\n'
            if args.output:
                descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, 'w') as output: output.write(data)
            else: print(data, end='')
            return 2 if args.check and result.get('has_changes') else 0
        except (httpx.HTTPError, ValueError, OSError, KeyError, RecursionError) as error:
            parser.exit(1, 'Policy command failed: ' + (str(error) if isinstance(error, ValueError) else type(error).__name__) + '\n')
        finally:
            if logged_in:
                try: client.post('api/v1/logout', json={})
                except httpx.HTTPError: pass


if __name__ == '__main__':
    raise SystemExit(main())
