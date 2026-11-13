"""Credential-free policy files, signed plans and atomic conditional application."""
import json
from typing import Annotated, Literal

from fastapi import Depends, HTTPException
from itsdangerous import BadData, URLSafeSerializer
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .access import current_principal
from .contracts import publication_impact
from .db import encode
from .fabric import GatewayInput
from .fleet import Mapping, fingerprint


class Manifest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    format: Literal['pliqra/policies/1'] = 'pliqra/policies/1'
    gateways: Annotated[list[GatewayInput], Field(min_length=1, max_length=50)]
    mapping: Mapping
    source_revision: Annotated[str, Field(pattern=r'^(?:[a-f0-9]{40}|[a-f0-9]{64})?$')] = ''

    @model_validator(mode='after')
    def unique(self):
        if len({g.slug for g in self.gateways}) != len(self.gateways):
            raise ValueError('Each gateway address must occur once')
        if len(encode(self.model_dump()).encode()) > 500000:
            raise ValueError('Policy file exceeds 500 KB')
        return self


class Apply(BaseModel):
    model_config = ConfigDict(extra='forbid')
    plan: Annotated[str, Field(min_length=1, max_length=2000000)]
    approve: Literal[True]


def resolve(app, document, mapping):
    value = document.model_dump()
    config = value['config']
    try:
        config['services'] = [mapping.services[i] for i in config['services']]
        for check in config['guardrails']:
            check['guardrail_id'] = mapping.guardrails[check['guardrail_id']]
        if config['project_id']:
            config['project_id'] = mapping.projects[config['project_id']]
        config['team_ids'] = [mapping.teams[i] for i in config['team_ids']]
    except KeyError:
        raise HTTPException(422, 'Map every service, guardrail, project and team reference explicitly') from None
    body = GatewayInput.model_validate(value)
    value['config'] = app.state.validate_gateway(body)
    return value


def desired_fingerprint(app, document):
    config = document['config']
    contracts = [app.state.gateway.fingerprint(app.state.gateway.port(i)) for i in config['services']]
    policy = app.state.gateway.policies.fingerprint({'id': 'policy-file', 'version': 0, 'config': config})
    return fingerprint({'document': document, 'contracts': contracts, 'policy': policy})


def current(app, slug):
    row = app.state.store.one('SELECT id FROM gateway_profiles WHERE slug=?', (slug,))
    if row and app.state.store.one('SELECT id FROM fleet_managed WHERE gateway_id=?', (row['id'],)):
        raise HTTPException(409, 'Use the Fleet review workflow for a fleet-managed gateway')
    return app.state.gateway.policies.profile(row['id']) if row else None


def mount(app):
    store, gateway, admin = app.state.store, lambda: app.state.gateway, app.state.context['admin']
    def signer():
        return URLSafeSerializer(app.state.vault.session_secret(), salt='pliqra-policy-plan/1')

    @app.post('/api/v1/policies/plan', tags=['Policy files'])
    def plan(body: Manifest, p=Depends(admin)):
        changes = []
        with store.transaction():
            for item in body.gateways:
                document = resolve(app, item, body.mapping)
                before = current(app, document['slug'])
                previous = {key: before[key] for key in ('name', 'slug', 'config')} if before else None
                action = 'create' if before is None else 'unchanged' if previous == document else 'update'
                after = {**(before or {}), 'id': before['id'] if before else 'new', 'version': before['version'] + 1 if before else 1, 'config': document['config']}
                changes.append({'slug': document['slug'], 'action': action, 'before': previous, 'after': document,
                                'expected_fingerprint': gateway().policies.fingerprint(before) if before else None,
                                'desired_fingerprint': desired_fingerprint(app, document),
                                'impact': publication_impact(gateway(), before, after, proposed_policy=after) if action == 'update' else None})
            expires = store.clock() + 600
            payload = {'format': 'pliqra/policy-plan/1', 'actor': p['id'], 'workspace': store.setting('workspace_id'),
                       'generation': gateway().generation, 'expires': expires, 'changes': changes,
                       'source_revision': body.source_revision, 'manifest_hash': fingerprint(body.model_dump())}
        token = signer().dumps(payload)
        return {'format': payload['format'], 'plan': token, 'changes': changes, 'expires_at': expires,
                'source_revision': body.source_revision, 'manifest_hash': payload['manifest_hash'],
                'has_changes': any(c['action'] != 'unchanged' for c in changes), 'credentials_included': False,
                'note': 'No resources changed. Review the complete diff and impact, then explicitly apply within ten minutes. Missing resources are never deleted.'}

    @app.post('/api/v1/policies/apply', tags=['Policy files'])
    def apply(body: Apply, p=Depends(admin)):
        try: payload = signer().loads(body.plan)
        except BadData: raise HTTPException(422, 'Invalid policy plan signature') from None
        with store.transaction():
            gateway().cluster.ensure_serving()
            actor = current_principal(store, p)
            if actor['role'] != 'admin' or payload['actor'] != actor['id'] or payload['workspace'] != store.setting('workspace_id'):
                raise HTTPException(403, 'This plan belongs to another administrator or workspace')
            if payload['generation'] != gateway().generation or payload['expires'] <= store.clock():
                raise HTTPException(412, 'Plan expired or workspace was restored; create a fresh plan')
            prepared = []
            for change in payload['changes']:
                before = current(app, change['slug'])
                actual = gateway().policies.fingerprint(before) if before else None
                if actual != change['expected_fingerprint']:
                    raise HTTPException(412, 'Gateway changed after planning; review a new plan')
                document = change['after']
                app.state.validate_gateway(GatewayInput.model_validate(document))
                if desired_fingerprint(app, document) != change['desired_fingerprint']:
                    raise HTTPException(412, 'A service contract or guardrail changed after planning')
                prepared.append((change, before))
            results = []
            for change, before in prepared:
                if change['action'] == 'unchanged':
                    results.append({'slug': change['slug'], 'action': 'unchanged', 'id': before['id'], 'version': before['version']})
                else:
                    saved = app.state.save_gateway(GatewayInput.model_validate(change['after']), actor, before['id'] if before else None, _locked=True)
                    results.append({'slug': saved['slug'], 'action': change['action'], 'id': saved['id'], 'version': saved['version']})
            store.audit(actor['id'], 'policy-file.applied', payload['manifest_hash'], {'source_revision': payload['source_revision'], 'changes': results})
        return {'applied': True, 'changes': results, 'source_revision': payload['source_revision'], 'manifest_hash': payload['manifest_hash']}
