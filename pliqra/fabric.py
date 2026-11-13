"""Management API for named gateways and reusable guardrails."""
import json
from contextlib import nullcontext
import sqlite3
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Request, Query
from pydantic import Field, StringConstraints

from .db import encode, now, uid
from .policies import Strict, GatewayConfig, GuardrailConfig, PolicyEngine
from .security import endpoint_url


class GatewayInput(Strict):
    name: Annotated[str, StringConstraints(strip_whitespace=True,min_length=1,max_length=100)]
    slug: Annotated[str, Field(pattern=r'^[a-z0-9][a-z0-9-]{0,62}$')]
    config: GatewayConfig


class GuardrailInput(Strict):
    name: Annotated[str, StringConstraints(strip_whitespace=True,min_length=1,max_length=100)]
    provider: Literal['builtin', 'nemo', 'webhook']
    config: GuardrailConfig
    api_key: Annotated[str | None, Field(max_length=4096)] = None
    enabled: bool = True


class Preview(Strict):
    service: str
    max_tokens: Annotated[int, Field(ge=1, le=131072)] = 512
    project_id: str | None = None
    team_id: str | None = None
    input_text: Annotated[str, Field(max_length=20000)] = 'Synthetic policy preview'
    stream: bool = True


class RailTest(Strict):
    text: Annotated[str, Field(min_length=1, max_length=20000)]
    stage: Literal['input', 'output'] = 'input'


def mount(app):
    ctx = app.state.context
    store, admin = ctx['store'], ctx['admin']
    engine = PolicyEngine(store, ctx['vault'])

    def validate_gateway(body):
        config = body.config.model_dump()
        if config['project_id'] and not store.one('SELECT id FROM projects WHERE id=?', (config['project_id'],)):
            raise HTTPException(422, 'Gateway project not found')
        if config['team_ids'] and not config['project_id']:
            raise HTTPException(422, 'Select a project before restricting a gateway to teams')
        for team in config['team_ids']:
            if not store.one('SELECT 1 FROM team_projects WHERE team_id=? AND project_id=?', (team, config['project_id'])):
                raise HTTPException(422, 'Every gateway team must be assigned to its project')
        services=[]
        for service in config['services']:
            port = app.state.gateway.port(service)
            services.append(port['id'])
            if port['project_id'] and config['project_id'] and port['project_id'] != config['project_id']:
                raise HTTPException(422, 'A private service must match the gateway project')
        config['services']=list(dict.fromkeys(services))
        for step in config['guardrails']:
            rail = store.one('SELECT * FROM guardrails WHERE id=?', (step['guardrail_id'],))
            if not rail:
                raise HTTPException(422, 'Guardrail not found')
            if rail['provider'] == 'builtin' and json.loads(rail['config'])['rule'] == 'json_schema' and 'input' in step['stages']:
                raise HTTPException(422, 'JSON Schema checks can only run on output')
        return config

    def public_gateway(profile):
        instant = now()
        stats = store.one("SELECT COUNT(*) AS requests,COALESCE(SUM(tokens),0) AS tokens,COALESCE(SUM(CASE WHEN status='blocked' THEN 1 ELSE 0 END),0) AS blocked,COALESCE(SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END),0) AS active FROM gateway_requests WHERE gateway_id=? AND started>?", (profile['id'], instant-86400))
        return {**profile, 'fingerprint': engine.fingerprint(profile), 'base_path': '/g/'+profile['slug']+'/v1', 'last_24h': stats}

    app.state.validate_gateway = validate_gateway

    def save_gateway(body, p, record_id=None, request=None, *, _locked=False):
        config = validate_gateway(body)
        previous = engine.profile(record_id) if record_id else None
        record_id, instant = record_id or uid('gw_'), now()
        fields = {'name': body.name.strip(), 'slug': body.slug, 'config': encode(config),
                  'version': previous['version']+1 if previous else 1, 'updated': instant}
        try:
            with (nullcontext() if _locked else store.transaction()):
                if previous:
                    previous = engine.profile(record_id)
                    fields['version'] = previous['version'] + 1
                    if request:
                        from .contracts import check_publication
                        check_publication(request, engine.fingerprint(previous))
                    store.update('gateway_profiles', record_id, fields)
                else:
                    store.insert('gateway_profiles', {'id': record_id, **fields, 'created': instant})
                store.insert('gateway_versions', {'gateway_id': record_id, 'version': fields['version'],
                             'document': encode(body.model_dump()), 'actor': p['id'], 'created': instant})
                store.audit(p['id'], 'gateway.updated' if previous else 'gateway.created', record_id, {'version': fields['version']})
        except sqlite3.IntegrityError:
            raise HTTPException(409, 'A gateway with this address already exists')
        return public_gateway(engine.profile(record_id))

    app.state.save_gateway = save_gateway

    @app.get('/api/v1/gateways', tags=['Gateways'])
    def gateways(p=Depends(admin)):
        return [public_gateway(engine.profile(row['id'])) for row in store.all('SELECT id FROM gateway_profiles ORDER BY created DESC')]

    @app.post('/api/v1/gateways', tags=['Gateways'], status_code=201)
    def create_gateway(body: GatewayInput, p=Depends(admin)):
        return save_gateway(body, p)

    @app.put('/api/v1/gateways/{record_id}', tags=['Gateways'])
    def update_gateway(record_id: str, body: GatewayInput, request: Request, p=Depends(admin)):
        return save_gateway(body, p, record_id, request)

    @app.post('/api/v1/gateways/{record_id}/impact', tags=['Gateways'])
    def impact(record_id: str, body: GatewayInput, limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0), p=Depends(admin)):
        from .contracts import publication_impact
        with store.transaction():
            previous = engine.profile(record_id)
            proposed = {**previous, 'version': previous['version'] + 1, 'config': validate_gateway(body)}
            return {'current_version': previous['version'], 'proposed_version': proposed['version'],
                    'current_fingerprint': engine.fingerprint(previous),
                    'impact': publication_impact(app.state.gateway, previous, proposed, proposed_policy=proposed,
                                                 limit=limit, offset=offset)}

    @app.get('/api/v1/gateways/{record_id}/versions', tags=['Gateways'])
    def versions(record_id: str, p=Depends(admin)):
        engine.profile(record_id)
        return [{**r, 'document': json.loads(r['document'])} for r in store.all('SELECT * FROM gateway_versions WHERE gateway_id=? ORDER BY version DESC', (record_id,))]

    @app.post('/api/v1/gateways/{record_id}/rollback/{version}', tags=['Gateways'])
    def rollback(record_id: str, version: int, p=Depends(admin)):
        row = store.one('SELECT document FROM gateway_versions WHERE gateway_id=? AND version=?', (record_id, version))
        if not row:
            raise HTTPException(404, 'Gateway version not found')
        return save_gateway(GatewayInput.model_validate_json(row['document']), p, record_id)

    @app.get('/api/v1/gateways/{record_id}/export', tags=['Gateways'])
    def export(record_id: str, p=Depends(admin)):
        profile = engine.profile(record_id)
        return {'schema': 'pliqra/gateway/1', 'name': profile['name'], 'slug': profile['slug'], 'config': profile['config'],
                'fingerprint': engine.fingerprint(profile), 'credentials_included': False,
                'references': 'Service and guardrail IDs refer to this workspace. Map them before creating a gateway on another instance.'}

    @app.post('/api/v1/gateways/{record_id}/preview', tags=['Gateways'])
    def preview(record_id: str, body: Preview, p=Depends(admin)):
        port = app.state.gateway.port(body.service)
        profile = engine.resolve(p, port, record_id)
        request = {'model': port['name'], 'messages': [{'role': 'user', 'content': body.input_text}],
                   'max_tokens': body.max_tokens, 'stream': body.stream,
                   'pliqra': {'project_id': body.project_id, 'team_id': body.team_id}}
        engine.preflight(profile, request, p)
        effective = {**port, 'verified_only': port.get('verified_only') or profile['config']['require_verified']}
        candidates, decisions = app.state.gateway.candidates(effective, ['chat'])
        reservation = (len(encode(request).encode())+1024+body.max_tokens)*len(candidates)
        values = {'principal': p['id'], 'user_id': p['id'], 'project_id': profile['config']['project_id'] or body.project_id,
                  'team_id': request['pliqra'].get('team_id'), 'port_id': port['id']}
        limits = [{**rule, 'used': engine.usage(profile, rule, values, now()),
                   'proposed': 1 if rule['metric'] == 'requests' else reservation} for rule in profile['config']['limits']]
        active = store.one("SELECT COUNT(*) AS n FROM gateway_requests WHERE gateway_id=? AND status='pending'", (profile['id'],))['n']
        return {'fingerprint': profile['fingerprint'], 'eligible': bool(candidates),
                'admissible': bool(candidates) and active < profile['config']['max_concurrency'] and all(r['used']+r['proposed'] <= r['limit'] for r in limits),
                'active_requests': active, 'limits': limits, 'routing': decisions, 'guardrails': profile['config']['guardrails'],
                'delivery': 'buffered_after_output_checks' if body.stream and engine.buffered(profile) else 'live_stream' if body.stream else 'json',
                'note': 'Administrator preview only. No inference, external guardrail call, quota reservation or real-key authorization test is performed.'}

    def public_rail(row):
        return {k: json.loads(v) if k == 'config' else v for k, v in row.items() if k != 'secret'} | {'has_secret': bool(row['secret'])}

    @app.get('/api/v1/guardrails', tags=['Guardrails'])
    def guardrails(p=Depends(admin)):
        return [public_rail(r) for r in store.all('SELECT * FROM guardrails ORDER BY created')]

    def save_rail(body, p, record_id=None):
        config = body.config.model_dump()
        if body.provider != 'builtin':
            config['endpoint'] = endpoint_url(config['endpoint'])
            if body.provider == 'nemo' and not config['config_id']:
                raise HTTPException(422, 'NeMo configuration ID is required')
        elif config['rule'] == 'keywords' and not config['terms']:
            raise HTTPException(422, 'Add at least one keyword or phrase')
        elif config['rule'] == 'pii' and not config['pii_types']:
            raise HTTPException(422, 'Select at least one PII pattern')
        elif config['rule'] == 'json_schema' and config['json_schema'] is None:
            raise HTTPException(422, 'Add a JSON Schema')
        previous = store.one('SELECT * FROM guardrails WHERE id=?', (record_id,)) if record_id else None
        if record_id and not previous:
            raise HTTPException(404, 'Guardrail not found')
        record_id, instant = record_id or uid('rail_'), now()
        secret = previous['secret'] if previous and body.api_key is None else ctx['vault'].encrypt(body.api_key or '')
        fields = {'name': body.name.strip(), 'provider': body.provider, 'config': encode(config), 'secret': secret,
                  'enabled': int(body.enabled), 'version': previous['version']+1 if previous else 1, 'updated': instant}
        with store.transaction():
            if previous:
                store.update('guardrails', record_id, fields)
            else:
                store.insert('guardrails', {'id': record_id, **fields, 'created': instant})
            store.audit(p['id'], 'guardrail.updated' if previous else 'guardrail.created', record_id, {'provider': body.provider, 'version': fields['version']})
        return public_rail(store.one('SELECT * FROM guardrails WHERE id=?', (record_id,)))

    @app.post('/api/v1/guardrails', tags=['Guardrails'], status_code=201)
    def create_rail(body: GuardrailInput, p=Depends(admin)):
        return save_rail(body, p)

    @app.put('/api/v1/guardrails/{record_id}', tags=['Guardrails'])
    def update_rail(record_id: str, body: GuardrailInput, p=Depends(admin)):
        return save_rail(body, p, record_id)

    @app.post('/api/v1/guardrails/{record_id}/test', tags=['Guardrails'])
    async def test_rail(record_id: str, body: RailTest, p=Depends(admin)):
        if not store.one('SELECT id FROM guardrails WHERE id=?', (record_id,)):
            raise HTTPException(404, 'Guardrail not found')
        role = 'user' if body.stage == 'input' else 'assistant'
        request = {'messages': [{'role': role, 'content': body.text}]}
        test_id=uid('railtest_')
        # Observe records the verdict but never blocks this explicit operator test.
        with app.state.gateway.checking():
            await app.state.gateway.policies.check(None, body.stage, request, app.state.gateway.client,event_id=test_id,
                               response={'choices': [{'message': request['messages'][0]}]},
                               only={'guardrail_id': record_id, 'stages': [body.stage], 'mode': 'observe', 'on_error': 'block'})
            store.audit(p['id'], 'guardrail.tested', record_id, {'stage': body.stage})
            return store.one('SELECT * FROM guardrail_decisions WHERE event_id=?', (test_id,))

    @app.get('/api/v1/guardrail-decisions', tags=['Guardrails'])
    def decisions(gateway_id: str = '', limit: int = 50, p=Depends(admin)):
        where,args=('WHERE gateway_id=?',[gateway_id]) if gateway_id else ('',[])
        return store.all('SELECT * FROM guardrail_decisions '+where+' ORDER BY created DESC LIMIT ?', [*args,max(1,min(limit,200))])

    @app.get('/g/{slug}/v1/models', tags=['Gateway inference'])
    def gateway_models(slug: str, p=Depends(ctx['principal'])):
        profile = engine.resolve(p, explicit=slug)
        models = []
        for service in profile['config']['services']:
            port = app.state.gateway.port(service)
            try:
                app.state.gateway.authorize_port(p, port)
            except HTTPException:
                continue
            if port['enabled']:
                models.append({'id': port['name'], 'object': 'model', 'created': int(port['created']), 'owned_by': 'pliqra'})
        return {'object': 'list', 'data': models}

    @app.get('/protocol/v1/gateway', tags=['Protocol'])
    def gateway_contract(gateway_id: str = '', p=Depends(ctx['principal'])):
        return {'gateway':engine.contract(engine.resolve(p,explicit=gateway_id or None))}

    @app.post('/g/{slug}/v1/chat/completions', tags=['Gateway inference'])
    async def gateway_chat(slug: str, body: ctx['ChatInput'], request: Request, p=Depends(ctx['principal'])):
        from .contracts import header_binding, BINDING_HEADER
        binding = header_binding(store, request)
        response = await app.state.gateway.chat(body.model_dump(exclude_none=True), p, binding, gateway_id=slug)
        if binding:
            response.headers[BINDING_HEADER] = binding['id']
        return response
