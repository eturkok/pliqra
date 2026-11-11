"""Opt-in HTTP binding protection and read-only publication impact reports."""
import json
import re

from fastapi import HTTPException
from starlette._utils import get_route_path
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from .access import current_principal
from .db import now

BINDING_HEADER = 'X-Pliqra-Binding'


def check_publication(request, fingerprint):
    expected = request.headers.getlist('If-Match')
    if expected and expected != ['"' + fingerprint + '"']:
        raise HTTPException(409, 'Publication preview is stale. Review the current impact report again.')


class BindingHeaderMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] == 'OPTIONS':
            return await self.app(scope, receive, send)
        values = Headers(scope=scope).getlist(BINDING_HEADER)
        if values:
            path = get_route_path(scope)
            supported = scope['method'] == 'POST' and (
                path == '/v1/chat/completions' or re.fullmatch(r'/g/[^/]+/v1/chat/completions', path))
            if len(values) != 1 or not re.fullmatch(r'bind_[A-Za-z0-9_-]{1,120}', values[0]) or not supported:
                response = JSONResponse({'detail': 'Use one X-Pliqra-Binding identifier on a Chat Completions endpoint.'},
                                        status_code=422, headers={'Cache-Control': 'no-store'})
                return await response(scope, receive, send)
            scope['pliqra.binding'] = values[0]
        await self.app(scope, receive, send)


def header_binding(store, request):
    record_id = request.scope.get('pliqra.binding')
    if not record_id:
        return None
    binding = store.one('SELECT * FROM bindings WHERE id=?', (record_id,))
    if not binding:
        raise HTTPException(403, 'Binding is missing or unavailable; explicitly connect again')
    return binding


def publication_impact(gateway, before, after, proposed_policy=None, limit=100, offset=0):
    """Assess retained live bindings without inference, guardrail IO or admission."""
    store = gateway.store
    clauses, args = ('gateway_id=?', [before['id']]) if proposed_policy else ('port_id=?', [before['id']])
    bindings = store.all('SELECT * FROM bindings WHERE ' + clauses + ' AND expires>? ORDER BY created DESC,id',
                         [*args, now()])
    counts = {'renew': 0, 'blocked': 0, 'unavailable': 0}
    items, excluded = [], 0
    # Multiple accepted contracts for one key can have different requirements.
    for binding in bindings:
        key = store.one('SELECT * FROM api_keys WHERE id=?', (binding['principal'],))
        actor = {'id': binding['principal'], 'kind': 'key' if key else 'user', 'role': 'consumer'}
        if key:
            actor['key'] = key
        port = gateway.port(binding['port_id']) if proposed_policy else before
        try:
            actor = current_principal(store, actor)
            current_policy = gateway.authorize_port(actor, port, binding['gateway_id'])
        except HTTPException:
            excluded += 1
            continue
        proposed = port if proposed_policy else after
        requirements = json.loads(binding['requirements'])
        decisions, reasons = [], []
        status, action = 'renew', 'Review the proposed contract and create a new binding after publication.'
        try:
            policy = gateway.authorize_port(actor, proposed, binding['gateway_id']) if not proposed_policy else (
                gateway.policies.resolve(actor, proposed, proposed_policy['id'], profile_override=proposed_policy))
            if policy:
                disabled = [cap for cap in requirements if cap in ('tools', 'vision') and not policy['config']['allow_' + cap]]
                if disabled:
                    raise HTTPException(422, 'Gateway disables required capabilities: ' + ', '.join(disabled))
            effective = {**proposed, 'verified_only': proposed.get('verified_only') or bool(policy and policy['config']['require_verified'])}
            candidates, decisions = gateway.candidates(effective, requirements)
            if not candidates:
                reasons = [d['reason'] for d in decisions if not d['eligible']]
                status = 'unavailable' if reasons and all('Temporarily unavailable' in reason for reason in reasons) else 'blocked'
                action = 'Restore eligible deployments or revise the requirements before reconnecting.'
        except HTTPException as exc:
            status, reasons = 'blocked', [str(exc.detail)]
            action = 'Restore service or gateway access before reconnecting.'
        counts[status] += 1
        team = store.one('SELECT id,name FROM teams WHERE id=?', (key.get('team_id'),)) if key and key.get('team_id') else None
        project_id = key['project_id'] if key else proposed.get('project_id')
        project = store.one('SELECT id,name FROM projects WHERE id=?', (project_id,)) if project_id else None
        if offset <= sum(counts.values()) - 1 < offset + limit:
            items.append({'binding_id': binding['id'], 'principal_id': actor['id'], 'kind': actor['kind'],
                          'application': key['name'] if key else actor['user']['name'], 'team': team, 'project': project,
                          'service': port['name'], 'requirements': requirements, 'status': status, 'reasons': reasons,
                          'action': action, 'decisions': decisions,
                          'already_stale': binding['fingerprint'] != gateway.fingerprint(port) or
                          binding['gateway_fingerprint'] != (current_policy['fingerprint'] if current_policy else None)})
    return {'summary': counts, 'total': sum(counts.values()), 'excluded_inactive_or_unauthorized': excluded,
            'items': items, 'offset': offset, 'limit': limit,
            'note': 'One row per unexpired authorized binding. Snapshot of configuration and observed readiness; quotas, external checks and model behavior are not exercised.'}
