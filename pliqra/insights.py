"""Read-only diagnosis and advisory application impact for observed behavior."""
import json
import socket
import ssl
from typing import Annotated

import httpx
from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .access import current_principal
from .behavior import endpoint_fingerprint
from .connectors import headers, response_json
from .gateway import CAPABILITIES, cost_micros
from .policies import reservation_units


class Diagnosis(BaseModel):
    model_config = ConfigDict(extra='forbid')
    service: Annotated[str, Field(max_length=256)] = ''
    connection_id: Annotated[str | None, Field(max_length=100)] = None
    key_id: Annotated[str | None, Field(max_length=100)] = None
    gateway_id: Annotated[str | None, Field(max_length=100)] = None
    binding_id: Annotated[str | None, Field(max_length=100)] = None
    requirements: Annotated[list[str], Field(max_length=8)] = ['chat']
    stream: bool = True
    max_tokens: Annotated[int, Field(ge=1, le=131072)] = 512
    input_chars: Annotated[int, Field(ge=0, le=1000000)] = 0
    inspect_endpoint: bool = False

    @model_validator(mode='after')
    def valid(self):
        if not self.service and not self.connection_id:
            raise ValueError('Select a service or an existing connection')
        if set(self.requirements) - CAPABILITIES:
            raise ValueError('Unknown capability')
        return self


def actor_for_key(store, key_id, fallback):
    if not key_id:
        return current_principal(store, fallback)
    return current_principal(store, {'id': key_id, 'kind': 'key', 'role': 'consumer'})


def quota_snapshot(gateway, principal, port, candidates, body, policy):
    """Conservative estimate before external transformations, without admission writes."""
    store, instant = gateway.store, gateway.store.clock()
    key = principal.get('key')
    context = body.get('pliqra') or {}
    project_id = key['project_id'] if key else port.get('project_id') or context.get('project_id')
    team_id = key.get('team_id') if key else context.get('team_id')
    project = store.one('SELECT * FROM projects WHERE id=?', (project_id,)) if project_id else None
    checks = []
    def add(name, used, proposed, limit):
        checks.append({'name': name, 'used': used, 'proposed': proposed, 'limit': limit,
                       'passed': used + proposed <= limit})
    runtime = store.setting('runtime', {}).get('max_concurrency', 64)
    active = store.one("SELECT COUNT(*) AS n FROM runtime_leases WHERE kind!='management'")['n']
    add('Workspace concurrency', active, 1, runtime)
    if key:
        used = store.one("SELECT COUNT(*) AS n FROM events WHERE key_id=? AND started>=? AND status!='rejected'", (key['id'], instant - 60))['n']
        add('Key requests per minute', used, 1, key['rpm'])
    if project:
        pending = store.one("SELECT COUNT(*) AS n FROM events WHERE project_id=? AND status='pending'", (project_id,))['n']
        add('Project concurrency', pending, 1, project['max_concurrency'])
        if project['monthly_budget'] is not None:
            priced = all(d['input_price'] is not None and d['output_price'] is not None for d in candidates)
            prices = {name: max(d[name] for d in candidates) for name in ('input_price', 'output_price')} if priced else None
            reserve = cost_micros(reservation_units(body), prices) * len(candidates) if priced else None
            checks.append({'name': 'Project budget', 'used': gateway.budget_usage(project_id, instant),
                           'proposed': reserve, 'limit': project['monthly_budget'] * 1000000,
                           'passed': priced and gateway.budget_usage(project_id, instant) + reserve <= project['monthly_budget'] * 1000000,
                           'reason': None if priced else 'Configure prices for every eligible deployment'})
    if policy:
        values = {'principal': principal['id'], 'user_id': key.get('created_by') if key else principal['id'],
                  'key_id': key['id'] if key else None, 'project_id': project_id,
                  'team_id': team_id, 'port_id': port['id']}
        pending = store.one("SELECT COUNT(*) AS n FROM gateway_requests WHERE gateway_id=? AND status='pending'", (policy['id'],))['n']
        add('Gateway concurrency', pending, 1, policy['config']['max_concurrency'])
        for rule in policy['config']['limits']:
            used = gateway.policies.usage(policy, rule, values, instant)
            proposed = 1 if rule['metric'] == 'requests' else sum(reservation_units(body)) * len(candidates)
            add('Gateway ' + rule['scope'] + ' ' + rule['metric'], used, proposed, rule['limit'])
    return checks


async def inspect_connection(app, record_id):
    gateway, store = app.state.gateway, app.state.store
    connection = store.one('SELECT * FROM connections WHERE id=?', (record_id,))
    if not connection:
        raise HTTPException(404, 'Connection not found')
    if not connection['enabled']:
        raise HTTPException(409, 'Connection is disabled')
    endpoint = connection['endpoint'].rstrip('/')
    path = '/api/tags' if connection['provider'] == 'ollama' else '/models'
    try:
        with gateway.checking():
            gateway.cluster.ensure_serving()
            auth = headers(await gateway.secrets.resolve(app.state.vault.decrypt(connection['secret'])), connection['auth_header'], connection['auth_prefix'])
            if connection['provider'] == 'anthropic': auth['anthropic-version'] = '2023-06-01'
            gateway.cluster.ensure_serving()
            async with app.state.client.stream('GET', endpoint + path, headers=auth, timeout=5) as response:
                status = response.status_code
                if status != 200:
                    category = 'credentials' if status in (401, 403) else 'base_url' if status == 404 else 'redirect' if 300 <= status < 400 else 'upstream_http'
                    return {'passed': False, 'category': category, 'http_status': status,
                            'action': {'credentials': 'Review the provider credential and authentication header.', 'base_url': 'Use the provider base URL, including /v1 where required.', 'redirect': 'Configure the final endpoint directly; credentials are never forwarded across redirects.'}.get(category, 'Inspect the provider availability and HTTP status.')}
                data = await response_json(response, 200000)
                entries = data.get('models' if connection['provider'] == 'ollama' else 'data') if isinstance(data, dict) else None
                return {'passed': isinstance(entries, list), 'category': 'discovery' if isinstance(entries, list) else 'discovery_format',
                        'model_count': len(entries) if isinstance(entries, list) else 0,
                        'action': 'Metadata discovery is available; no inference capability was verified.' if isinstance(entries, list) else 'Check the provider adapter or configure the model explicitly.'}
    except httpx.TimeoutException:
        category = 'timeout'
    except httpx.ConnectError as error:
        causes, current = [], error
        while current and len(causes) < 8:
            causes.append(current); current = current.__cause__
        category = 'tls' if any(isinstance(e, ssl.SSLError) or 'CERTIFICATE_VERIFY_FAILED' in str(e) for e in causes) else 'dns' if any(isinstance(e, socket.gaierror) for e in causes) else 'connectivity'
    except (httpx.HTTPError, ValueError, json.JSONDecodeError):
        category = 'discovery_format'
    return {'passed': False, 'category': category, 'action': {'tls': 'Mount the trusted CA bundle with PLIQRA_CA_BUNDLE; retain certificate validation.', 'dns': 'Check DNS from the gateway container or pod.', 'timeout': 'Check network policy and provider response time.', 'connectivity': 'Check the endpoint listener, container routing and firewall. Host models in Docker use host.docker.internal.'}.get(category, 'Check the adapter and bounded model-list response.')}


def behavior_impact(gateway, schedule_id, limit=100, offset=0):
    store = gateway.store
    schedule = store.one('SELECT * FROM behavior_schedules WHERE id=?', (schedule_id,))
    if not schedule:
        raise HTTPException(404, 'Behavior-check schedule not found')
    config = json.loads(gateway.vault.decrypt(schedule['config']))
    observed = bool(schedule['last_fingerprint'])
    stale = observed and schedule['last_fingerprint'] != endpoint_fingerprint(gateway, schedule['deployment_id'], config['project_id'])
    capabilities = {check['name']: check['capability'] for check in config['checks']}
    results = json.loads(schedule['last_results']) if not stale else []
    failures = [r for r in results if r.get('outcome') in ('failed', 'error')]
    failed = {capabilities[r['name']] for r in failures if r['name'] in capabilities}
    items, counts, excluded = [], {'review_required': 0, 'alternative_available': 0, 'requirements_not_matched': 0}, 0
    from .quality import active_release
    ports, targets = {}, {}
    bindings = store.all('SELECT * FROM bindings WHERE expires>? ORDER BY created DESC,id LIMIT 10001', (store.clock(),)) if failed else []
    for binding in bindings[:10000]:
        if binding['port_id'] not in ports:
            try: ports[binding['port_id']] = gateway.port(binding['port_id'])
            except HTTPException: ports[binding['port_id']] = None
            port = ports[binding['port_id']]
            targets[binding['port_id']] = set(port['deployments']) if port else set()
            release = active_release(store, port['id']) if port else None
            if release:
                try: targets[port['id']].update(gateway.port(release['candidate_id'])['deployments'])
                except HTTPException: pass
        port = ports[binding['port_id']]
        if not port or schedule['deployment_id'] not in targets[port['id']]:
            continue
        key = store.one('SELECT * FROM api_keys WHERE id=?', (binding['principal'],))
        try:
            actor = actor_for_key(store, key['id'], {}) if key else current_principal(store, {'id': binding['principal'], 'kind': 'user', 'role': 'consumer'})
            policy = gateway.authorize_port(actor, port, binding['gateway_id'])
        except HTTPException:
            excluded += 1; continue
        if not failed:
            continue
        required = set(json.loads(binding['requirements'])) | set(port['requirements'])
        matched = not required or bool(required & failed) or 'chat' in failed and bool(required & {'chat', 'streaming', 'tools', 'vision', 'structured_output'})
        effective = {**port, 'verified_only': port.get('verified_only') or bool(policy and policy['config']['require_verified'])}
        candidates, _ = gateway.candidates(effective, required)
        if policy and any(cap in required and not policy['config']['allow_' + cap] for cap in ('tools', 'vision')):
            candidates = []
        alternatives = [{'id': d['id'], 'model': d['model']} for d in candidates if d['id'] != schedule['deployment_id']]
        status = 'requirements_not_matched' if not matched else 'alternative_available' if alternatives else 'review_required'
        counts[status] += 1
        if offset <= sum(counts.values()) - 1 < offset + limit:
            items.append({'binding_id': binding['id'], 'application': key['name'] if key else actor['user']['name'],
                          'principal_id': actor['id'], 'project_id': key['project_id'] if key else port.get('project_id'),
                          'team_id': key.get('team_id') if key else None, 'service': port['name'], 'requirements': sorted(required),
                          'status': status, 'alternatives': alternatives,
                          'already_stale': binding['fingerprint'] != gateway.fingerprint(port) or binding['gateway_fingerprint'] != (policy['fingerprint'] if policy else None)})
    return {'schedule_id': schedule_id, 'observation_stale': stale, 'observation_state': 'not_tested' if not observed else 'stale' if stale else 'current', 'failed_capabilities': sorted(failed),
            'summary': counts, 'total': sum(counts.values()), 'items': items, 'offset': offset, 'limit': limit,
            'excluded_inactive_or_unauthorized': excluded, 'scan_truncated': len(bindings) > 10000,
            'note': 'Advisory snapshot of retained authorized bindings, including execution errors. Alternatives are configuration-eligible, not behavior-certified. Unbound applications are unknown. No routing, capabilities or contracts were changed.'}


def mount(app):
    store, admin = app.state.store, app.state.context['admin']

    @app.post('/api/v1/diagnostics', tags=['Diagnostics'])
    async def diagnose(body: Diagnosis, principal=Depends(admin)):
        gateway, checks, candidates = app.state.gateway, [], []
        connection_id = body.connection_id
        phase = 'identity'
        try:
            actor = actor_for_key(store, body.key_id, principal)
            checks.append({'phase': phase, 'passed': True, 'message': 'Identity is currently active'})
            if connection_id:
                connection = store.one('SELECT id,enabled FROM connections WHERE id=?', (connection_id,))
                if not connection: raise HTTPException(404, 'Connection not found')
                if not connection['enabled']: raise HTTPException(409, 'Connection is disabled')
                checks.append({'phase': 'connection_configuration', 'passed': True, 'message': 'Stored connection exists; endpoint reachability is not assessed without discovery.'})
            if body.service:
                phase = 'access_contract_and_routing'
                binding = store.one('SELECT * FROM bindings WHERE id=?', (body.binding_id,)) if body.binding_id else None
                if body.binding_id and not binding: raise HTTPException(403, 'Binding not found')
                request = {'model': body.service, 'messages': [{'role': 'user', 'content': 'x' * body.input_chars}], 'stream': body.stream, 'max_tokens': body.max_tokens}
                actor = current_principal(store, actor)
                request, port, candidates, decisions, policy, _ = gateway.prepare(request, actor, binding, gateway_id=body.gateway_id, required_capabilities=body.requirements)
                if policy and any(cap in body.requirements and not policy['config']['allow_' + cap] for cap in ('tools', 'vision')):
                    raise HTTPException(422, 'Gateway disables a requested capability')
                gateway.cluster.ensure_serving()
                if gateway.maintenance: raise HTTPException(503, 'Workspace maintenance is in progress')
                checks.append({'phase': phase, 'passed': True, 'routing': decisions})
                connection_id = connection_id or candidates[0]['connection_id']
                phase = 'quota_and_budget'
                quotas = quota_snapshot(gateway, actor, port, candidates, request, policy)
                checks.append({'phase': phase, 'passed': all(q['passed'] for q in quotas), 'limits': quotas})
        except HTTPException as error:
            detail = error.detail if isinstance(error.detail, str) else error.detail.get('message', 'Configuration does not currently permit this request')
            checks.append({'phase': phase, 'passed': False, 'status': error.status_code, 'message': detail,
                           'routing': error.detail.get('decisions', []) if isinstance(error.detail, dict) else [],
                           'action': 'Review the selected identity, grants and gateway assignment; renew stale bindings after review. For quota failures, inspect current usage and reservations.'})
        if body.inspect_endpoint and connection_id:
            try: result = await inspect_connection(app, connection_id)
            except HTTPException as error: result = {'passed': False, 'category': 'configuration', 'status': error.status_code, 'action': 'Review the connection, Secret reference and workspace readiness.'}
            checks.append({'phase': 'endpoint_metadata', **result})
        return {'ready': (all(c['passed'] for c in checks) if body.service or body.inspect_endpoint or any(not c['passed'] for c in checks) else None), 'checks': checks, 'model_calls': 0,
                'endpoint_checked': any(c['phase'] == 'endpoint_metadata' for c in checks),
                'note': 'Snapshot only. No inference, quota reservation, guardrail execution or capability verification. Endpoint discovery is opt-in. Expanded guardrail input and concurrent admission can change the final result.'}

    @app.get('/api/v1/diagnostics/runtime', tags=['Diagnostics'])
    def runtime(p=Depends(admin)):
        return {'telemetry': app.state.telemetry.status(), 'login_protection': {'shared': True, 'backend': store.backend, 'attempts': 15, 'window_seconds': 300, 'trusted_proxy_networks': len(app.state.login_control.trusted)}}

    @app.get('/api/v1/behavior-checks/{record_id}/impact', tags=['Behavior checks'])
    def impact(record_id: str, limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0), p=Depends(admin)):
        return behavior_impact(app.state.gateway, record_id, limit, offset)
