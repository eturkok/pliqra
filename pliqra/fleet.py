"""Explicit independent-instance trust, immutable proposals and audited policy delivery."""
import asyncio
import hashlib
import json
import secrets
from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .connectors import response_json
from .db import encode, now, uid
from .fabric import GatewayInput
from .security import digest, endpoint_url, bearer_token


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()).hexdigest()


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class TrustInput(Strict):
    name: Annotated[str, Field(min_length=1, max_length=100)]
    prefix: Annotated[str, Field(min_length=1, max_length=40, pattern=r'^[a-z0-9][a-z0-9-]*$')] = 'fleet-'
    scopes: Annotated[list[Literal['status', 'policy']], Field(min_length=1, max_length=2)] = ['status']


class MemberInput(Strict):
    name: Annotated[str, Field(min_length=1, max_length=100)]
    endpoint: Annotated[str, Field(max_length=2000)]
    token: Annotated[str | None, Field(min_length=32, max_length=256)] = None
    enabled: bool = True


class Mapping(Strict):
    services: Annotated[dict[str, str], Field(max_length=100)]
    guardrails: Annotated[dict[str, str], Field(max_length=100)] = {}
    projects: Annotated[dict[str, str], Field(max_length=100)] = {}
    teams: Annotated[dict[str, str], Field(max_length=100)] = {}


class PlanInput(Strict):
    document: GatewayInput
    mapping: Mapping


class AgentPlan(Strict):
    format: Literal['pliqra-fleet-plan/1'] = 'pliqra-fleet-plan/1'
    workspace_id: str
    trust_id: str
    document: GatewayInput
    previous_document: GatewayInput | None = None
    expected_fingerprint: str | None
    desired_fingerprint: Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
    expires: float


class ApplyInput(Strict):
    operation_id: Annotated[str, Field(min_length=1, max_length=100, pattern=r'^[a-zA-Z0-9_-]+$')]
    plan: AgentPlan
    author: Annotated[str, Field(min_length=1, max_length=100)]
    approver: Annotated[str, Field(min_length=1, max_length=100)]


class TargetInput(Strict):
    member_id: str
    mapping: Mapping


class ProposalInput(Strict):
    name: Annotated[str, Field(min_length=1, max_length=100)]
    document: GatewayInput
    targets: Annotated[list[TargetInput], Field(min_length=1, max_length=20)]


class Decision(Strict):
    fingerprint: Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
    approve: bool = True


class SnapshotModel(BaseModel):
    model_config=ConfigDict(extra='ignore',allow_inf_nan=False)


class Totals(SnapshotModel):
    requests:Annotated[int,Field(ge=0)]=0
    successful:Annotated[int,Field(ge=0)]=0
    input_tokens:Annotated[int,Field(ge=0)]=0
    output_tokens:Annotated[int,Field(ge=0)]=0
    known_cost_micros:Annotated[int,Field(ge=0)]=0
    unknown_usage:Annotated[int,Field(ge=0)]=0
    unknown_cost:Annotated[int,Field(ge=0)]=0


class TeamUsage(Totals):
    id:Annotated[str,Field(max_length=100)]|None=None
    name:Annotated[str,Field(max_length=256)]


class InventoryItem(SnapshotModel):
    id:Annotated[str,Field(min_length=1,max_length=100)]
    name:Annotated[str,Field(min_length=1,max_length=256)]
    fingerprint:Annotated[str,Field(pattern=r'^[a-f0-9]{64}$')]|None=None
    provider:Annotated[str,Field(max_length=32)]|None=None
    version:int|None=None
    enabled:int|None=None


class PolicyInventory(SnapshotModel):
    guardrails:Annotated[list[InventoryItem],Field(max_length=200)]=[]
    projects:Annotated[list[InventoryItem],Field(max_length=200)]=[]
    teams:Annotated[list[InventoryItem],Field(max_length=200)]=[]


class ServiceItem(InventoryItem):
    fingerprint:Annotated[str,Field(pattern=r'^[a-f0-9]{64}$')]


class ManagedGateway(SnapshotModel):
    slug:Annotated[str,Field(max_length=63)]
    fingerprint:Annotated[str,Field(pattern=r'^[a-f0-9]{64}$')]|None=None
    desired_fingerprint:Annotated[str,Field(pattern=r'^[a-f0-9]{64}$')]|None=None
    drifted:bool
    configuration_available:bool=True


class FleetSite(SnapshotModel):
    id:Annotated[str,Field(max_length=63)]
    region:Annotated[str,Field(max_length=63)]
    mode:Literal['active','draining','standby']


class Snapshot(SnapshotModel):
    format:Literal['pliqra-fleet-status/1']
    workspace_id:Annotated[str,Field(min_length=1,max_length=100)]
    version:Annotated[str,Field(max_length=64)]
    as_of:float
    window_seconds:Literal[86400]
    ready:bool
    scopes:Annotated[list[Literal['status','policy']],Field(max_length=2)]
    sites:Annotated[list[FleetSite],Field(max_length=200)]
    totals:Totals
    teams:Annotated[list[TeamUsage],Field(max_length=2000)]
    services:Annotated[list[ServiceItem],Field(max_length=200)]
    service_total:Annotated[int,Field(ge=0)]
    policy_inventory:PolicyInventory
    managed_gateways:Annotated[list[ManagedGateway],Field(max_length=500)]
    raw_content_included:Literal[False]


def clean_snapshot(value):
    try:return Snapshot.model_validate(value).model_dump()
    except ValidationError:raise HTTPException(502,'Fleet member returned an invalid metadata snapshot') from None


def peer_url(value):
    result = endpoint_url(value)
    parts = urlsplit(result)
    if parts.scheme != 'https' and parts.hostname not in ('127.0.0.1', 'localhost', '::1'):
        raise HTTPException(422, 'Fleet connections require HTTPS; loopback HTTP is available for local acceptance')
    if parts.path and not (parts.path.startswith('/org/') and parts.path.count('/') == 2):
        raise HTTPException(422, 'Use an instance root or /org/organization base URL')
    return result


def mount(app):
    store, vault, admin = app.state.store, app.state.vault, app.state.context['admin']
    with store.transaction():
        workspace = store.setting('workspace_id')
        if not workspace:
            workspace = uid('workspace_')
            store.set_setting('workspace_id', workspace)

    def workspace_id():
        # Read through restore; no closure may retain a replaced workspace identity.
        store.run("INSERT INTO settings (key,value) VALUES ('workspace_id',?) ON CONFLICT(key) DO NOTHING",(encode(uid('workspace_')),))
        return store.setting('workspace_id')

    def public_trust(row):
        return {k: json.loads(v) if k == 'scopes' else v for k, v in row.items() if k != 'token_hash'}

    def trust(request, scope):
        token = bearer_token(request.headers.get('authorization', '')) or ''
        row = store.one('SELECT * FROM fleet_trusts WHERE token_hash=? AND active=1', (digest(token),)) if token.startswith('plq_fleet_') else None
        if not row:
            raise HTTPException(401, 'Invalid or revoked fleet pairing')
        if scope not in json.loads(row['scopes']):
            raise HTTPException(403, 'This pairing does not grant the requested fleet capability')
        app.state.gateway.cluster.ensure_live()
        store.update('fleet_trusts', row['id'], {'last_used': store.clock()})
        return row

    def current(slug, grant):
        if not slug.startswith(grant['prefix']):
            raise HTTPException(403, 'Gateway address is outside the pairing namespace')
        row = store.one('SELECT id FROM gateway_profiles WHERE slug=?', (slug,))
        managed = store.one('SELECT * FROM fleet_managed WHERE id=?', (slug,))
        if row and (not managed or managed['trust_id'] != grant['id'] or managed['gateway_id'] != row['id']):
            raise HTTPException(409, 'This address belongs to a locally managed gateway or a different pairing')
        return app.state.gateway.policies.profile(row['id']) if row else None

    def desired(document):
        body = GatewayInput.model_validate(document)
        resolved = body.model_dump()
        resolved['config'] = app.state.validate_gateway(body)
        services = [app.state.gateway.fingerprint(app.state.gateway.port(i)) for i in resolved['config']['services']]
        policy = app.state.gateway.policies.fingerprint({'id': 'fleet-plan', 'version': 0, 'config': resolved['config']})
        return resolved, fingerprint({'document': resolved, 'service_contracts': services, 'policy': policy})

    def plan(body, grant):
        doc = body.document.model_dump()
        mapping = body.mapping.model_dump()
        config = doc['config']
        try:
            config['services'] = [mapping['services'][v] for v in config['services']]
            for step in config['guardrails']:
                step['guardrail_id'] = mapping['guardrails'][step['guardrail_id']]
            if config['project_id']:
                config['project_id'] = mapping['projects'][config['project_id']]
            config['team_ids'] = [mapping['teams'][v] for v in config['team_ids']]
        except KeyError:
            raise HTTPException(422, 'Map every service, guardrail, project and team alias explicitly') from None
        previous = current(doc['slug'], grant)
        document, state = desired(doc)
        return AgentPlan(workspace_id=workspace_id(), trust_id=grant['id'], document=document,
                         previous_document={k: previous[k] for k in ('name','slug','config')} if previous else None,
                         expected_fingerprint=app.state.gateway.policies.fingerprint(previous) if previous else None,
                         desired_fingerprint=state, expires=store.clock() + 3600).model_dump()

    @app.get('/api/v1/fleet/trusts', tags=['Fleet'])
    def trusts(p=Depends(admin)):
        return [public_trust(r) for r in store.all('SELECT * FROM fleet_trusts ORDER BY created')]

    @app.post('/api/v1/fleet/trusts', tags=['Fleet'], status_code=201)
    def pair(body: TrustInput, p=Depends(admin)):
        token = 'plq_fleet_' + secrets.token_urlsafe(36)
        row = {'id': uid('trust_'), 'name': body.name.strip(), 'prefix': body.prefix, 'scopes': encode(sorted(set(body.scopes))),
               'token_hash': digest(token), 'active': 1, 'created': store.clock(), 'last_used': None}
        with store.transaction():
            if store.one('SELECT COUNT(*) AS n FROM fleet_trusts WHERE active=1')['n'] >= 50:
                raise HTTPException(422, 'At most 50 active controller pairings are supported')
            store.insert('fleet_trusts', row)
            store.audit(p['id'], 'fleet.paired', row['id'], {'prefix': body.prefix, 'scopes': body.scopes})
        return {**public_trust(row), 'token': token, 'shown_once': True}

    @app.delete('/api/v1/fleet/trusts/{record_id}', tags=['Fleet'])
    def revoke(record_id: str, p=Depends(admin)):
        with store.transaction():
            if not store.one('SELECT id FROM fleet_trusts WHERE id=?', (record_id,)):
                raise HTTPException(404)
            store.update('fleet_trusts', record_id, {'active': 0})
            store.audit(p['id'], 'fleet.pairing_revoked', record_id)
        return {'revoked': True}

    @app.get('/api/v1/fleet-agent/status', tags=['Fleet agent'])
    def agent_status(request: Request):
        grant = trust(request, 'status')
        since = store.clock() - 86400
        columns = "COUNT(*) AS requests,COALESCE(SUM(CASE WHEN status='success' THEN 1 ELSE 0 END),0) AS successful,COALESCE(SUM(input_tokens),0) AS input_tokens,COALESCE(SUM(output_tokens),0) AS output_tokens,COALESCE(SUM(known_cost_micros),0) AS known_cost_micros,COALESCE(SUM(CASE WHEN input_tokens IS NULL OR output_tokens IS NULL THEN 1 ELSE 0 END),0) AS unknown_usage,COALESCE(SUM(CASE WHEN cost_micros IS NULL THEN 1 ELSE 0 END),0) AS unknown_cost"
        with store.transaction():
            totals = store.one('SELECT ' + columns + ' FROM events WHERE started>=?', (since,))
            teams = store.all("SELECT e.team_id AS id,COALESCE(t.name,'Unattributed') AS name,COUNT(*) AS requests,COALESCE(SUM(e.input_tokens),0) AS input_tokens,COALESCE(SUM(e.output_tokens),0) AS output_tokens,COALESCE(SUM(e.known_cost_micros),0) AS known_cost_micros,COALESCE(SUM(CASE WHEN e.input_tokens IS NULL OR e.output_tokens IS NULL THEN 1 ELSE 0 END),0) AS unknown_usage,COALESCE(SUM(CASE WHEN e.cost_micros IS NULL THEN 1 ELSE 0 END),0) AS unknown_cost FROM events e LEFT JOIN teams t ON t.id=e.team_id WHERE e.started>=? GROUP BY e.team_id,t.name", (since,))
            services = [{'id': r['id'], 'name': r['name'], 'fingerprint': app.state.gateway.fingerprint(app.state.gateway.port(r['id']))} for r in store.all('SELECT id,name FROM ports ORDER BY name LIMIT 200')]
            managed = []
            for row in store.all('SELECT * FROM fleet_managed WHERE trust_id=?', (grant['id'],)):
                try:
                    profile = app.state.gateway.policies.profile(row['gateway_id'])
                    doc = {k: profile[k] for k in ('name', 'slug', 'config')}
                    _, state = desired(doc)
                    managed.append({'slug': row['id'], 'fingerprint': app.state.gateway.policies.fingerprint(profile),
                                    'desired_fingerprint': state, 'drifted': state != row['desired_fingerprint'] or app.state.gateway.policies.fingerprint(profile) != row['applied_fingerprint']})
                except HTTPException:
                    managed.append({'slug':row['id'],'drifted':True,'configuration_available':False})
        try:
            app.state.gateway.cluster.ensure_serving()
            ready = True
        except HTTPException:
            ready = False
        from . import __version__
        return {'format': 'pliqra-fleet-status/1', 'workspace_id': workspace_id(), 'version': __version__,
                'as_of': store.clock(), 'window_seconds': 86400, 'ready': ready, 'scopes': json.loads(grant['scopes']),
                'sites': store.all('SELECT id,region,mode FROM sites ORDER BY id'), 'totals': totals, 'teams': teams,
                'services': services, 'service_total':store.one('SELECT COUNT(*) AS n FROM ports')['n'],
                'policy_inventory':{'guardrails':store.all('SELECT id,name,provider,version,enabled FROM guardrails ORDER BY name LIMIT 200'),
                                    'projects':store.all('SELECT id,name FROM projects ORDER BY name LIMIT 200'),
                                    'teams':store.all('SELECT id,name FROM teams ORDER BY name LIMIT 200')},
                'managed_gateways': managed, 'raw_content_included': False}

    @app.post('/api/v1/fleet-agent/plan', tags=['Fleet agent'])
    def agent_plan(body: PlanInput, request: Request):
        grant = trust(request, 'policy')
        with store.transaction():
            return plan(body, grant)

    @app.post('/api/v1/fleet-agent/apply', tags=['Fleet agent'])
    def agent_apply(body: ApplyInput, request: Request):
        grant = trust(request, 'policy')
        submitted = body.plan.model_dump()
        plan_hash = fingerprint(submitted)
        receipt_id = grant['id'] + '_' + body.operation_id
        with store.transaction():
            if not store.one('SELECT id FROM fleet_trusts WHERE id=? AND active=1',(grant['id'],)):
                raise HTTPException(401,'Fleet pairing was revoked')
            app.state.gateway.cluster.ensure_live()
            existing = store.one('SELECT * FROM fleet_receipts WHERE id=?', (receipt_id,))
            if existing:
                if existing['plan_hash'] != plan_hash:
                    raise HTTPException(409, 'Operation identifier was already used for a different plan')
                return {**json.loads(existing['receipt']), 'idempotent_replay': True}
            if body.author == body.approver:
                raise HTTPException(403, 'Policy author and approver must be different people')
            if body.plan.workspace_id != workspace_id() or body.plan.trust_id != grant['id']:
                raise HTTPException(409, 'Plan belongs to another workspace or pairing')
            if body.plan.expires <= store.clock() or body.plan.expires > store.clock() + 3601:
                raise HTTPException(409, 'Policy plan has expired or has an invalid expiry')
            previous = current(body.plan.document.slug, grant)
            if not previous and store.one('SELECT COUNT(*) AS n FROM fleet_managed WHERE trust_id=?',(grant['id'],))['n']>=500:
                raise HTTPException(422,'At most 500 gateways may be managed by one controller pairing')
            actual = app.state.gateway.policies.fingerprint(previous) if previous else None
            if actual != body.plan.expected_fingerprint:
                raise HTTPException(409, 'Gateway changed after planning; prepare a new proposal')
            document, state = desired(body.plan.document.model_dump())
            if state != body.plan.desired_fingerprint:
                raise HTTPException(409, 'Mapped services or guardrails changed after planning')
            record_id, instant = previous['id'] if previous else uid('gw_'), store.clock()
            fields = {'name': document['name'], 'slug': document['slug'], 'config': encode(document['config']),
                      'version': previous['version'] + 1 if previous else 1, 'updated': instant}
            if previous:
                store.update('gateway_profiles', record_id, fields)
            else:
                store.insert('gateway_profiles', {'id': record_id, **fields, 'created': instant})
            store.insert('gateway_versions', {'gateway_id': record_id, 'version': fields['version'], 'document': encode(document), 'actor': 'fleet:' + grant['id'], 'created': instant})
            applied = app.state.gateway.policies.fingerprint(app.state.gateway.policies.profile(record_id))
            ownership = {'id': document['slug'], 'trust_id': grant['id'], 'gateway_id': record_id, 'desired_fingerprint': state, 'applied_fingerprint': applied, 'updated': instant}
            if store.one('SELECT id FROM fleet_managed WHERE id=?', (document['slug'],)):
                store.update('fleet_managed', document['slug'], {k: v for k, v in ownership.items() if k != 'id'})
            else:
                store.insert('fleet_managed', ownership)
            receipt = {'workspace_id': workspace_id(), 'gateway_id': record_id, 'version': fields['version'], 'slug': document['slug'],
                       'fingerprint': applied, 'desired_fingerprint': state, 'operation_id': body.operation_id, 'applied': instant}
            store.run('DELETE FROM fleet_receipts WHERE created<?', (instant - 90 * 86400,))
            store.insert('fleet_receipts', {'id': receipt_id, 'trust_id': grant['id'], 'plan_hash': plan_hash, 'receipt': encode(receipt), 'created': instant})
            store.audit('fleet:' + grant['id'], 'fleet.policy_applied', record_id, {'operation_id': body.operation_id, 'author': body.author, 'approver': body.approver, 'fingerprint': applied})
            return receipt

    def member(record_id):
        row = store.one('SELECT * FROM fleet_members WHERE id=?', (record_id,))
        if not row:
            raise HTTPException(404, 'Fleet member not found')
        return row

    def public_member(row):
        return {k: json.loads(v) if k == 'snapshot' else v for k, v in row.items() if k != 'secret'}

    async def remote(row, path, body=None):
        if not row['enabled']:
            raise HTTPException(409, 'Fleet member is paused')
        try:
            async with asyncio.timeout(15):
                request = app.state.client.build_request('POST' if body is not None else 'GET', row['endpoint'] + '/api/v1/fleet-agent/' + path,
                                                        json=body, headers={'Authorization': 'Bearer ' + vault.decrypt(row['secret'])})
                response = await app.state.client.send(request, stream=True, follow_redirects=False)
                try:
                    if response.status_code >= 300:
                        raise HTTPException(409 if response.status_code == 409 else 502, 'Remote policy conflict; replan' if response.status_code == 409 else 'Fleet member denied access or could not complete the operation')
                    result = await response_json(response, limit=1_000_000)
                finally:
                    await response.aclose()
            if result.get('workspace_id') != row['workspace_id']:
                raise HTTPException(409, 'Fleet endpoint now identifies a different workspace; pair it explicitly again')
            return result
        except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError, TimeoutError):
            raise HTTPException(502, 'Fleet connection failed; inspect pairing, TLS and member availability') from None

    async def sync(row):
        claim=uid('sync_')
        with store.transaction():
            live=member(row['id'])
            if live['sync_until']>store.clock():raise HTTPException(409,'Member synchronization is already running')
            store.update('fleet_members',row['id'],{'sync_claim':claim,'sync_until':store.clock()+30})
        try:
            snapshot = clean_snapshot(await remote(row, 'status'))
            with store.transaction():
                live=member(row['id'])
                if live['sync_claim']!=claim or any(live[k]!=row[k] for k in ('endpoint','secret','enabled','workspace_id')):
                    raise HTTPException(409,'Member changed during synchronization; refresh it again')
                store.update('fleet_members', row['id'], {'snapshot': encode(snapshot), 'last_sync': store.clock(), 'last_error': None})
        except HTTPException as exc:
            store.update('fleet_members', row['id'], {'last_error': str(exc.detail)})
            raise
        finally:
            store.run('UPDATE fleet_members SET sync_claim=NULL,sync_until=0,next_sync=? WHERE id=? AND sync_claim=?',(store.clock()+60,row['id'],claim))
        return public_member(member(row['id']))

    app.state.fleet_sync=sync

    @app.get('/api/v1/fleet', tags=['Fleet'])
    def fleet(p=Depends(admin)):
        return {'members': [public_member(r) for r in store.all('SELECT * FROM fleet_members ORDER BY created')],
                'proposals': [proposal(r['id']) for r in store.all('SELECT id FROM fleet_proposals ORDER BY created DESC LIMIT 100')],
                'trusts': [public_trust(r) for r in store.all('SELECT * FROM fleet_trusts ORDER BY created')]}

    @app.post('/api/v1/fleet/members', tags=['Fleet'], status_code=201)
    async def add_member(body: MemberInput, p=Depends(admin)):
        if not body.token or not body.token.startswith('plq_fleet_'):
            raise HTTPException(422, 'Use the one-time pairing token created by the member administrator')
        if store.one('SELECT COUNT(*) AS n FROM fleet_members')['n'] >= 50:
            raise HTTPException(422, 'At most 50 independent members are supported')
        row = {'id': uid('member_'), 'name': body.name.strip(), 'endpoint': peer_url(body.endpoint), 'secret': vault.encrypt(body.token), 'workspace_id': '', 'enabled': 1, 'created': store.clock()}
        # Initial identity is discovered only through the explicitly supplied pairing.
        try:
            request = app.state.client.build_request('GET', row['endpoint'] + '/api/v1/fleet-agent/status', headers={'Authorization': 'Bearer ' + body.token})
            async with asyncio.timeout(15):
                response = await app.state.client.send(request, stream=True, follow_redirects=False)
                try:
                    if response.status_code != 200:
                        raise HTTPException(502, 'Fleet pairing was not accepted by the member')
                    snapshot = await response_json(response, limit=1_000_000)
                finally:
                    await response.aclose()
            snapshot=clean_snapshot(snapshot)
        except (httpx.HTTPError, ValueError, TypeError, KeyError, TimeoutError):
            raise HTTPException(502, 'Fleet pairing failed; inspect TLS, token and member availability') from None
        if snapshot['workspace_id'] == workspace_id():
            raise HTTPException(422, 'Pair a separate workspace, not this controller itself')
        row.update(workspace_id=snapshot['workspace_id'], snapshot=encode(snapshot), last_sync=store.clock(), last_error=None, enabled=int(body.enabled),next_sync=store.clock()+60)
        with store.transaction():
            if store.one('SELECT id FROM fleet_members WHERE workspace_id=?',(row['workspace_id'],)):
                raise HTTPException(409,'This workspace is already registered; use one shared ingress for its replicas to avoid double-counting')
            store.insert('fleet_members', row)
            store.audit(p['id'], 'fleet.member_added', row['id'], {'workspace_id': row['workspace_id']})
        return public_member(row)

    @app.put('/api/v1/fleet/members/{record_id}', tags=['Fleet'])
    async def edit_member(record_id: str, body: MemberInput, p=Depends(admin)):
        row = member(record_id)
        if peer_url(body.endpoint) != row['endpoint']:
            raise HTTPException(409, 'Pair a new member to change its endpoint identity')
        fields = {'name': body.name.strip(), 'enabled': int(body.enabled)}
        if body.token:
            if not body.token.startswith('plq_fleet_'):
                raise HTTPException(422, 'Invalid fleet pairing token')
            candidate = {**row, 'secret': vault.encrypt(body.token), 'enabled': 1}
            await remote(candidate, 'status')
            fields['secret'] = candidate['secret']
        with store.transaction():
            store.update('fleet_members', record_id, fields)
            store.audit(p['id'], 'fleet.member_updated', record_id, {'enabled': body.enabled, 'token_rotated': bool(body.token)})
        return public_member(member(record_id))

    @app.post('/api/v1/fleet/members/{record_id}/sync', tags=['Fleet'])
    async def sync_member(record_id: str, p=Depends(admin)):
        return await sync(member(record_id))

    @app.post('/api/v1/fleet/sync', tags=['Fleet'])
    async def sync_all(p=Depends(admin)):
        results = []
        for row in store.all('SELECT * FROM fleet_members WHERE enabled=1 ORDER BY created'):
            try:
                await sync(row)
                results.append({'member_id': row['id'], 'status': 'synced'})
            except HTTPException:
                results.append({'member_id': row['id'], 'status': 'unavailable'})
        return {'results': results}

    @app.get('/api/v1/fleet/summary', tags=['Fleet'])
    def summary(member_id: str = '', team_id: str = '', p=Depends(admin)):
        if team_id and not member_id:
            raise HTTPException(422, 'A team filter requires its member; independent team identifiers are not merged')
        totals = dict(requests=0, input_tokens=0, output_tokens=0, known_cost_micros=0, unknown_usage=0, unknown_cost=0)
        included, excluded, teams = [], [], []
        rows = store.all('SELECT * FROM fleet_members' + (' WHERE id=?' if member_id else ''), (member_id,) if member_id else ())
        for row in rows:
            if not row['enabled'] or row['last_error'] or not row['last_sync'] or row['last_sync'] < store.clock() - 300:
                excluded.append(row['id'])
                continue
            snapshot = json.loads(row['snapshot'])
            included.append(row['id'])
            selected = next((t for t in snapshot['teams'] if t['id'] == team_id), {}) if team_id else snapshot['totals']
            for key in totals:
                totals[key] += int(selected.get(key) or 0)
            teams.extend({**t, 'member_id': row['id'], 'member_name': row['name']} for t in snapshot['teams'] if not team_id or t['id'] == team_id)
        return {'window_seconds': 86400, 'as_of': store.clock(), 'freshness_seconds': 300, 'included_members': included, 'excluded_members': excluded,
                'totals': totals, 'teams': teams, 'costs_are_estimates': True, 'snapshot_reporting': True}

    def proposal(record_id):
        row = store.one('SELECT * FROM fleet_proposals WHERE id=?', (record_id,))
        if not row:
            raise HTTPException(404, 'Fleet proposal not found')
        return {**row, 'document': json.loads(row['document']),
                'targets': [{**r, 'plan': json.loads(r['plan']), 'receipt': json.loads(r['receipt']) if r['receipt'] else None} for r in store.all('SELECT * FROM fleet_targets WHERE proposal_id=? ORDER BY id', (record_id,))]}

    @app.post('/api/v1/fleet/proposals', tags=['Fleet'], status_code=201)
    async def propose(body: ProposalInput, p=Depends(admin)):
        if len({t.member_id for t in body.targets}) != len(body.targets):
            raise HTTPException(422, 'Each member must occur once in a proposal')
        targets = []
        for target in body.targets:
            row = member(target.member_id)
            planned = await remote(row, 'plan', {'document': body.document.model_dump(), 'mapping': target.mapping.model_dump()})
            checked = AgentPlan.model_validate(planned)
            targets.append({'member_id': row['id'], 'plan': checked.model_dump(), 'member': row})
        instant, record_id = store.clock(), uid('proposal_')
        content = {'document': body.document.model_dump(), 'targets': [{k: v for k, v in t.items() if k != 'member'} for t in targets]}
        with store.transaction():
            for target in targets:
                current_member = member(target['member_id'])
                if any(current_member[k] != target['member'][k] for k in ('endpoint', 'workspace_id', 'secret', 'enabled')):
                    raise HTTPException(409, 'Member changed while planning; prepare the proposal again')
            store.insert('fleet_proposals', {'id': record_id, 'name': body.name.strip(), 'document': encode(content), 'fingerprint': fingerprint(content), 'author': p['id'], 'status': 'pending', 'created': instant})
            for target in targets:
                store.insert('fleet_targets', {'id': uid('target_'), 'proposal_id': record_id, 'member_id': target['member_id'], 'plan': encode(target['plan']), 'updated': instant})
            store.audit(p['id'], 'fleet.proposed', record_id, {'fingerprint': fingerprint(content), 'targets': len(targets)})
        return proposal(record_id)

    @app.post('/api/v1/fleet/proposals/{record_id}/decision', tags=['Fleet'])
    def decide(record_id: str, body: Decision, p=Depends(admin)):
        with store.transaction():
            row = proposal(record_id)
            if row['status'] != 'pending' or row['fingerprint'] != body.fingerprint:
                raise HTTPException(409, 'Proposal changed or was already decided')
            if row['author'] == p['id']:
                raise HTTPException(403, 'A different administrator must review this proposal')
            if any(t['plan']['expires'] <= store.clock() for t in row['targets']):
                raise HTTPException(409, 'A member plan expired; prepare a new proposal')
            store.update('fleet_proposals', record_id, {'status': 'approved' if body.approve else 'rejected', 'approved_by': p['id'], 'approved': store.clock()})
            store.audit(p['id'], 'fleet.approved' if body.approve else 'fleet.rejected', record_id, {'fingerprint': row['fingerprint']})
        return proposal(record_id)

    @app.post('/api/v1/fleet/proposals/{record_id}/deploy', tags=['Fleet'])
    async def deploy(record_id: str, p=Depends(admin)):
        row = proposal(record_id)
        if row['status'] != 'approved':
            raise HTTPException(409, 'Only approved immutable proposals can be deployed')
        for actor in (row['author'],row['approved_by']):
            if not store.one("SELECT id FROM users WHERE id=? AND active=1 AND role='admin'",(actor,)):
                raise HTTPException(403,'Proposal author and approver must retain active administrator access')
        for target in row['targets']:
            claim = uid('claim_')
            with store.transaction():
                app.state.gateway.cluster.ensure_live()
                live = store.one('SELECT * FROM fleet_targets WHERE id=?', (target['id'],))
                if live['status'] == 'applied':
                    continue
                if live['claim_until'] > store.clock():
                    raise HTTPException(409, 'Another operator is delivering this target')
                if live['status'] in ('conflict', 'denied'):
                    continue
                store.update('fleet_targets', target['id'], {'status': 'applying', 'claim': claim, 'claim_until': store.clock() + 60, 'updated': store.clock()})
            status, receipt, error = 'unknown', None, None
            try:
                receipt = await remote(member(target['member_id']), 'apply', {'operation_id': target['id'], 'plan': target['plan'], 'author': row['author'], 'approver': row['approved_by']})
                if receipt.get('operation_id') != target['id'] or receipt.get('desired_fingerprint') != target['plan']['desired_fingerprint']:
                    raise HTTPException(502, 'Member returned an invalid policy receipt')
                status = 'applied'
            except HTTPException as exc:
                status = 'conflict' if exc.status_code == 409 else 'unknown'
                error = str(exc.detail)
            finally:
                with store.transaction():
                    app.state.gateway.cluster.ensure_live()
                    live = store.one('SELECT claim FROM fleet_targets WHERE id=?', (target['id'],))
                    if live and live['claim'] == claim:
                        store.update('fleet_targets', target['id'], {'status': status, 'receipt': encode(receipt) if receipt else None, 'last_error': error, 'claim_until': 0, 'updated': store.clock()})
                        store.audit(p['id'], 'fleet.delivery_' + status, target['id'], {'proposal_id': record_id})
        return proposal(record_id)

    @app.post('/api/v1/fleet/proposals/{record_id}/rollback-proposal',tags=['Fleet'])
    async def rollback_proposal(record_id:str,p=Depends(admin)):
        old=proposal(record_id)
        if any(t['status'] in ('unknown','applying') for t in old['targets']):
            raise HTTPException(409,'Reconcile uncertain policy deliveries before planning a rollback')
        targets=[]
        for target in old['targets']:
            if target['status']!='applied':continue
            document=target['plan'].get('previous_document')
            if not document:
                document=json.loads(encode(target['plan']['document']))
                document['config']['enabled']=False
            config=document['config']
            mapping={'services':{v:v for v in config['services']},'guardrails':{r['guardrail_id']:r['guardrail_id'] for r in config['guardrails']},
                     'projects':{config['project_id']:config['project_id']} if config['project_id'] else {},'teams':{v:v for v in config['team_ids']}}
            row=member(target['member_id'])
            planned=await remote(row,'plan',{'document':document,'mapping':mapping})
            targets.append({'member_id':row['id'],'plan':AgentPlan.model_validate(planned).model_dump()})
        if not targets:raise HTTPException(409,'No confirmed applied targets can be rolled back; reconcile uncertain targets first')
        content={'rollback_of':record_id,'targets':targets}
        instant,new_id=store.clock(),uid('proposal_')
        with store.transaction():
            store.insert('fleet_proposals',{'id':new_id,'name':('Roll back '+old['name'])[:100],'document':encode(content),'fingerprint':fingerprint(content),'author':p['id'],'status':'pending','created':instant})
            for target in targets:store.insert('fleet_targets',{'id':uid('target_'),'proposal_id':new_id,'member_id':target['member_id'],'plan':encode(target['plan']),'updated':instant})
            store.audit(p['id'],'fleet.rollback_proposed',new_id,{'rollback_of':record_id})
        return proposal(new_id)


async def worker(app):
    while True:
        await asyncio.sleep(10)
        if app.state.gateway.maintenance:continue
        store=app.state.store
        rows=store.all('SELECT * FROM fleet_members WHERE enabled=1 AND sync_until<=? AND next_sync<=? ORDER BY next_sync,created LIMIT 5',(store.clock(),store.clock()))
        async def refresh(row):
            try:
                with app.state.gateway.cluster.checking('management'):
                    await app.state.fleet_sync(row)
            except HTTPException:pass
        await asyncio.gather(*(refresh(row) for row in rows))
