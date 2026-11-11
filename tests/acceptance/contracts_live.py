"""Exercise 2.3 contracts with an official client and a real running text model.

Requires a disposable empty Pliqra workspace and an OpenAI-compatible model.
Never run against an existing workspace. No credentials or model text enter the report.
"""
import argparse
import importlib.metadata
import json
import secrets
import subprocess
import os
from pathlib import Path

import httpx2 as httpx
import openai


def run(args):
    checks = []
    with httpx.Client(base_url=args.gateway, timeout=60, trust_env=False) as admin:
        setup = admin.post('/api/v1/setup', json={'name': 'Contract acceptance', 'email': 'acceptance@example.test',
                                                'password': secrets.token_urlsafe(32)})
        assert setup.status_code == 200, 'Use a disposable empty workspace'
        admin.headers['X-Pliqra-CSRF'] = setup.json()['csrf']
        gateway_version = admin.get('/api/v1/settings').json()['version']

        def post(path, body):
            result = admin.post(path, json=body)
            assert result.is_success, (path, result.status_code, result.text)
            return result.json()

        connection = post('/api/v1/connections', {'name': 'Real local model', 'endpoint': args.endpoint,
                                                 'provider': 'openai', 'manual_model': args.model})
        service = next(item for item in admin.get('/api/v1/ports').json() if item['id'] == connection['services'][0]['id'])
        profile = post('/api/v1/gateways', {'name': 'Contract gateway', 'slug': 'contract-test',
                                           'config': {'services': [service['id']]}})
        connected = post('/api/v1/application-connections', {'name': 'Summary application', 'service': service['name'],
                         'gateway_id': profile['id'], 'requirements': ['chat', 'streaming']})
        credential, binding = connected['key']['token'], connected['binding']
        messages = [{'role': 'user', 'content': 'Say hello in one short sentence.'}]

        def consumer(binding_id, base='/v1'):
            return openai.OpenAI(api_key=credential, base_url=args.gateway + base, max_retries=0,
                                 default_headers={'X-Pliqra-Binding': binding_id})

        def javascript(binding_id, expected_status=200):
            if not args.javascript_modules:
                return
            payload = {'key': credential, 'binding': binding_id, 'baseURL': args.gateway + '/v1',
                       'model': service['name'], 'expectedStatus': expected_status}
            result = subprocess.run([args.node, str(Path(__file__).with_name('contracts_client.cjs'))],
                                    input=json.dumps(payload), text=True, capture_output=True, check=True,
                                    env={**os.environ, 'NODE_PATH': str(args.javascript_modules)})
            assert json.loads(result.stdout)['passed']

        with consumer(binding['id']) as client:
            result = client.chat.completions.create(model=service['name'], messages=messages, max_tokens=24)
            assert result.choices[0].message.content
            checks.append('official_python_json')
            chunks = list(client.chat.completions.create(model=service['name'], messages=messages, max_tokens=24, stream=True))
            assert any(chunk.choices and chunk.choices[0].delta.content for chunk in chunks)
            checks.append('official_python_stream')
        with consumer(binding['id'], '/g/contract-test/v1') as client:
            assert client.chat.completions.create(model=service['name'], messages=messages, max_tokens=24).choices
            checks.append('named_gateway_header')

        javascript(binding['id'])
        if args.javascript_modules:
            checks.append('official_javascript_json_and_stream')

        # A real endpoint can be published with declared or independently verified capabilities.
        # This candidate intentionally omits streaming; no ability is inferred from the model name.
        candidate = post('/api/v1/connections', {'name': 'Restricted candidate', 'endpoint': args.endpoint,
                                                'provider': 'openai', 'manual_model': args.model})
        candidate_connection = next(item for item in admin.get('/api/v1/connections').json() if item['id'] == candidate['id'])
        candidate_id = candidate_connection['models'][0]['id']
        updated = admin.patch('/api/v1/deployments/' + candidate_id, json={'capabilities': ['chat']})
        assert updated.is_success
        proposed = {key: service[key] for key in ('name', 'description', 'requirements', 'strategy', 'enabled', 'project_id',
                                                  'verified_only', 'cache_ttl', 'max_input_chars', 'max_output_tokens', 'log_content', 'placement')}
        proposed['deployments'] = [candidate_id]
        before = admin.get('/api/v1/events').json()['total']
        report = post('/api/v1/ports/' + service['id'] + '/preview', proposed)
        assert report['impact']['summary']['blocked'] == 1
        assert report['impact']['items'][0]['application'] == 'Summary application'
        assert admin.get('/api/v1/events').json()['total'] == before
        checks.append('publication_report_blocks_missing_streaming_without_inference')

        # A compatible configuration change needs review and explicit renewal.
        proposed['deployments'] = service['deployments']
        proposed['description'] = 'Reviewed contract revision'
        report = post('/api/v1/ports/' + service['id'] + '/preview', proposed)
        assert report['impact']['summary']['renew'] == 1
        published = admin.patch('/api/v1/ports/' + service['id'], json=proposed,
                                headers={'If-Match': '"' + report['current_fingerprint'] + '"'})
        assert published.is_success
        with consumer(binding['id']) as client:
            try:
                client.chat.completions.create(model=service['name'], messages=messages, max_tokens=24)
                raise AssertionError('Stale contract was accepted')
            except openai.APIStatusError as error:
                assert error.status_code == 409
        javascript(binding['id'], 409)
        if args.javascript_modules:
            checks.append('official_javascript_changed_contract_409')
        event = admin.get('/api/v1/events').json()['items'][0]
        assert event['usage_source'] == 'not_invoked' and event['status'] == 'rejected'
        checks.append('changed_contract_409_before_model')
        fresh = admin.post('/protocol/v1/bindings', headers={'Authorization': 'Bearer ' + credential},
                           json={'service': service['name'], 'requirements': ['chat', 'streaming']})
        assert fresh.status_code == 201
        with consumer(fresh.json()['id']) as client:
            assert client.chat.completions.create(model=service['name'], messages=messages, max_tokens=24).choices
        javascript(fresh.json()['id'])
        checks.append('explicit_renewal_restores_existing_client')
        document = {'name': profile['name'], 'slug': profile['slug'], 'config': {**profile['config'], 'enabled': False}}
        policy_report = post('/api/v1/gateways/' + profile['id'] + '/impact', document)
        assert policy_report['impact']['summary']['blocked'] == 2
        checks.append('policy_impact_reports_access_loss')

    return {'passed': True, 'synthetic_provider': False, 'model': args.model, 'runtime': args.runtime_label, 'gateway_version': gateway_version,
            'client': {'openai_python': importlib.metadata.version('openai'), 'openai_javascript': json.loads((args.javascript_modules / 'openai/package.json').read_text())['version'] if args.javascript_modules else None}, 'checks': checks,
            'scope': 'Real local text inference. Capability rejection uses explicitly restricted candidate metadata; model tool quality and geographic HA were not assessed.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gateway', required=True)
    parser.add_argument('--endpoint', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--runtime-label', default='User-supplied OpenAI-compatible runtime')
    parser.add_argument('--node', default='node')
    parser.add_argument('--javascript-modules', type=Path)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    report = run(args)
    args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'passed': True, 'checks': len(report['checks'])}))
