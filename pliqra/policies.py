"""Composable gateway admission and content checks, independent of HTTP routes."""
import asyncio
import copy
import hashlib
import json
import re
import time
from typing import Annotated, Literal

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .access import require_project
from .db import encode, now, uid
from .security import endpoint_url
from .inference_schema import map_message_text, requests_tools, INFERENCE_CONTRACT_REVISION
from .schema_validation import SchemaExecutor, validate_schema_definition


def reservation_units(body):
    # Internal unit hints are produced only by typed non-chat adapters, never accepted on chat APIs.
    return (max(len(encode(body).encode('utf-8')),body.get('_input_units',0))+1024,
            max(body.get('max_completion_tokens',body.get('max_tokens',1024)),body.get('_output_units',0)))


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Quota(Strict):
    metric: Literal['requests', 'tokens']
    scope: Literal['gateway', 'key', 'user', 'project', 'team', 'service'] = 'gateway'
    window_seconds: Annotated[int, Field(ge=1, le=2592000)] = 60
    limit: Annotated[int, Field(ge=1, le=10**12)]


class RailStep(Strict):
    guardrail_id: str
    stages: Annotated[list[Literal['input', 'output']], Field(min_length=1, max_length=2)] = ['input']
    mode: Literal['enforce', 'observe'] = 'enforce'
    on_error: Literal['block', 'allow'] = 'block'


class GatewayConfig(Strict):
    services: Annotated[list[str], Field(min_length=1, max_length=100)]
    project_id: str | None = None
    team_ids: Annotated[list[str], Field(max_length=100)] = []
    limits: Annotated[list[Quota], Field(max_length=30)] = []
    guardrails: Annotated[list[RailStep], Field(max_length=20)] = []
    max_concurrency: Annotated[int, Field(ge=1, le=200)] = 20
    max_input_chars: Annotated[int, Field(ge=1, le=1000000)] = 200000
    max_output_tokens: Annotated[int, Field(ge=1, le=131072)] = 4096
    require_verified: bool = False
    allow_tools: bool = True
    allow_vision: bool = True
    enabled: bool = True


class GuardrailConfig(Strict):
    endpoint: Annotated[str, Field(max_length=2000)] = ''
    config_id: Annotated[str, Field(max_length=200)] = ''
    timeout_seconds: Annotated[float, Field(ge=.1, le=60)] = 10
    auth_header: Annotated[str, Field(pattern=r'^[A-Za-z][A-Za-z0-9-]{0,63}$')] = 'Authorization'
    auth_prefix: Annotated[str, Field(max_length=32, pattern=r'^[A-Za-z0-9 -]*$')] = 'Bearer'
    rule: Literal['keywords', 'pii', 'json_schema'] = 'keywords'
    terms: Annotated[list[Annotated[str, Field(min_length=1, max_length=200)]], Field(max_length=100)] = []
    pii_types: list[Literal['email', 'phone']] = ['email']
    action: Literal['block', 'redact'] = 'block'
    json_schema: dict | None = None

    @model_validator(mode='after')
    def valid_schema(self):
        if self.json_schema is not None:
            validate_schema_definition(self.json_schema)
        return self


PII = {
    'email': re.compile(r'\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b', re.I),
    'phone': re.compile(r'(?<!\w)\+?\d[\d ()-]{7,}\d(?!\w)'),
}
MAX_CHECKED_BYTES = 4_000_000


class PolicyEngine:
    def __init__(self, store, vault):
        self.store, self.vault = store, vault
        self.schemas=SchemaExecutor()

    def profile(self, identity):
        row = self.store.one('SELECT * FROM gateway_profiles WHERE id=? OR slug=?', (identity, identity))
        if not row:
            raise HTTPException(404, 'Gateway not found')
        return {**row, 'config': json.loads(row['config'])}

    def fingerprint(self, profile):
        rails = []
        for step in profile['config']['guardrails']:
            row = profile['rail_snapshots'].get(step['guardrail_id']) if 'rail_snapshots' in profile else self.store.one('SELECT * FROM guardrails WHERE id=?', (step['guardrail_id'],))
            rails.append({k:row[k] for k in ('id','provider','config','version','enabled')} if row else None)
        value = {'id': profile['id'], 'version': profile['version'], 'config': profile['config'], 'guardrails': rails,
                 'contract_revision':INFERENCE_CONTRACT_REVISION}
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def resolve(self, principal, port=None, explicit=None, profile_override=None):
        assigned = (principal.get('key') or {}).get('gateway_id')
        profile = profile_override or (self.profile(explicit or assigned) if explicit or assigned else None)
        if not profile:
            return None
        if assigned and assigned != profile['id']:
            raise HTTPException(403, 'This key is assigned to a different gateway')
        config = profile['config']
        if not config['enabled']:
            raise HTTPException(403, 'Gateway is disabled')
        if config['project_id']:
            require_project(self.store, principal, config['project_id'])
        if config['team_ids'] and principal['role'] != 'admin':
            if principal['kind'] == 'key':
                allowed = principal['key'].get('team_id') in config['team_ids']
            else:
                allowed = any(self.store.one('SELECT 1 FROM team_members WHERE user_id=? AND team_id=?',
                                            (principal['id'], team)) for team in config['team_ids'])
            if not allowed:
                raise HTTPException(403, 'Gateway requires membership or a key attributed to an allowed team')
        if port and port['id'] not in config['services']:
            raise HTTPException(403, 'Service is not published by this gateway')
        # Hold a coherent per-request guardrail snapshot across asynchronous checks.
        with self.store.lock:
            profile['rail_snapshots']={s['guardrail_id']:self.store.one('SELECT * FROM guardrails WHERE id=?',(s['guardrail_id'],)) for s in config['guardrails']}
            profile['fingerprint'] = self.fingerprint(profile)
        return profile

    def preflight(self, profile, body, principal):
        if not profile:
            return
        config = profile['config']
        context = body.setdefault('pliqra', {})
        if not isinstance(context, dict):
            raise HTTPException(422, 'pliqra must be an object')
        if len(encode(body['messages'])) > config['max_input_chars']:
            raise HTTPException(422, 'Gateway input character limit exceeded')
        if body.get('max_completion_tokens', body.get('max_tokens', 1024)) > config['max_output_tokens']:
            raise HTTPException(422, 'Gateway output token limit exceeded')
        if body.get('n', 1) != 1 or body.get('best_of', 1) != 1:
            raise HTTPException(422, 'Managed gateways require one completion per request')
        if requests_tools(body) and not config['allow_tools']:
            raise HTTPException(403, 'Tool calling is disabled by this gateway')
        if not config['allow_vision'] and any(isinstance(m.get('content'), list) and any(
                part.get('type') in ('image_url', 'input_image') for part in m['content']) for m in body['messages']):
            raise HTTPException(403, 'Image inputs are disabled by this gateway')
        if config['project_id']:
            if context.get('project_id') and context['project_id'] != config['project_id']:
                raise HTTPException(403, 'Request attribution must match the gateway project')
            context['project_id'] = config['project_id']
        if config['team_ids']:
            key = principal.get('key')
            team = key.get('team_id') if key else context.get('team_id')
            if not team and not key and len(config['team_ids']) == 1:
                team = context['team_id'] = config['team_ids'][0]
            if team not in config['team_ids']:
                raise HTTPException(403, 'Use an allowed gateway team for attribution')

    def contract(self, profile):
        if not profile:
            return None
        config=profile['config']
        checks=[]
        for step in config['guardrails']:
            row=profile['rail_snapshots'].get(step['guardrail_id']) if 'rail_snapshots' in profile else self.store.one('SELECT * FROM guardrails WHERE id=?',(step['guardrail_id'],))
            checks.append({**step,**({k:row[k] for k in ('name','provider','version','enabled')} if row else {'enabled':False})})
        return {'id':profile['id'],'name':profile['name'],'version':profile['version'],
                'contract_revision':INFERENCE_CONTRACT_REVISION,
                'fingerprint':self.fingerprint(profile),'limits':config['limits'],
                'max_concurrency':config['max_concurrency'],'max_input_chars':config['max_input_chars'],
                'max_output_tokens':config['max_output_tokens'],'allow_tools':config['allow_tools'],
                'allow_vision':config['allow_vision'],'require_verified':config['require_verified'],
                'guardrails':checks,'stream_delivery':'guarded_buffer' if self.buffered(profile) else 'live_stream'}

    def usage(self, profile, rule, values, instant):
        column = {'key': 'key_id', 'user': 'user_id', 'project': 'project_id', 'team': 'team_id', 'service': 'port_id'}.get(rule['scope'])
        where, args = 'gateway_id=? AND started>?', [profile['id'], instant-rule['window_seconds']]
        if column:
            where += ' AND '+column+' IS NOT DISTINCT FROM ?'
            args.append(values.get(column))
        expression = 'COUNT(*)' if rule['metric'] == 'requests' else 'COALESCE(SUM(tokens),0)'
        return int(self.store.one('SELECT '+expression+' AS n FROM gateway_requests WHERE '+where, args)['n'])

    def admit(self, profile, event_id, principal, port, body, project_id, team_id, attempts):
        """Called inside the inference admission transaction; no quota race or second transaction."""
        if not profile:
            return
        latest = self.profile(profile['id'])
        if self.fingerprint(latest) != profile['fingerprint']:
            raise HTTPException(409, 'Gateway policy changed during admission; retry with the current contract')
        instant = self.store.clock()
        config = profile['config']
        pending = self.store.one("SELECT COUNT(*) AS n FROM gateway_requests WHERE gateway_id=? AND status='pending'", (profile['id'],))['n']
        if pending >= config['max_concurrency']:
            raise HTTPException(429, 'Gateway concurrent request limit reached', headers={'Retry-After': '1'})
        key = principal.get('key')
        values = {'principal': principal['id'], 'user_id': key.get('created_by') if key else principal['id'], 'key_id': key['id'] if key else None,
                  'project_id': project_id, 'team_id': team_id, 'port_id': port['id']}
        # Admission is conservative, not a tokenizer: reserve input UTF-8 bytes and requested output for every possible attempt.
        reserve = sum(reservation_units(body))*attempts
        for rule in config['limits']:
            used = self.usage(profile, rule, values, instant)
            amount = 1 if rule['metric'] == 'requests' else reserve
            if used+amount > rule['limit']:
                raise HTTPException(429, {'code': 'gateway_quota_exceeded', 'metric': rule['metric'], 'scope': rule['scope'],
                                         'limit': rule['limit'], 'used': used, 'reservation': amount,
                                         'window_seconds': rule['window_seconds']},
                                    headers={'Retry-After': str(rule['window_seconds'])})
        self.store.insert('gateway_requests', {'id': event_id, 'gateway_id': profile['id'],
                          'fingerprint': profile['fingerprint'], **values, 'started': instant,
                          'status': 'pending', 'tokens': reserve, 'reservation': reserve})

    def settle(self, event_id, status, attempts):
        ledger = self.store.one('SELECT * FROM gateway_requests WHERE id=?', (event_id,))
        if not ledger:
            return
        complete = all(a['input_tokens'] is not None and a['output_tokens'] is not None for a in attempts)
        known = sum((a['input_tokens'] or 0)+(a['output_tokens'] or 0) for a in attempts)
        tokens = known if complete else max(known, ledger['reservation'])
        self.store.update('gateway_requests', event_id, {'tokens': tokens, 'status': status})

    def refresh_reservation(self, profile, event_id, body, attempts):
        # Called inside the budget transaction; never count a second request.
        if not profile:return
        ledger=self.store.one('SELECT * FROM gateway_requests WHERE id=?',(event_id,))
        reserve=sum(reservation_units(body))*attempts
        if reserve<=ledger['reservation']:return
        instant=self.store.clock()
        for rule in profile['config']['limits']:
            if rule['metric']!='tokens' or ledger['started']<=instant-rule['window_seconds']:continue
            used=self.usage(profile,rule,ledger,instant)-ledger['tokens']
            if used+reserve>rule['limit']:
                raise HTTPException(429,{'code':'gateway_quota_exceeded','metric':'tokens','scope':rule['scope'],
                    'limit':rule['limit'],'used':used,'reservation':reserve,'window_seconds':rule['window_seconds']},
                    headers={'Retry-After':str(rule['window_seconds'])})
        self.store.update('gateway_requests',event_id,{'tokens':reserve,'reservation':reserve})

    def buffered(self, profile):
        return bool(profile and any('output' in step['stages'] for step in profile['config']['guardrails']))

    async def check(self, profile, stage, body, client, event_id=None, response=None, only=None):
        value = copy.deepcopy(body['messages'] if stage == 'input' else [c['message'] for c in response['choices']])
        for step in ([only] if only else profile['config']['guardrails']):
            if stage not in step['stages']:
                continue
            rail = profile['rail_snapshots'].get(step['guardrail_id']) if profile and 'rail_snapshots' in profile else self.store.one('SELECT * FROM guardrails WHERE id=?', (step['guardrail_id'],))
            started = time.perf_counter()
            outcome, changed = 'passed', value
            try:
                if not rail or not rail['enabled']:
                    raise ValueError('Guardrail unavailable')
                config = json.loads(rail['config'])
                outcome, changed = await self.evaluate(rail, config, stage, value, body, client)
            except Exception:
                # Adapter/validator failures are decisions under the explicit error policy.
                # Cancellation is a BaseException and still propagates to request cleanup.
                outcome = 'error'
            self.store.insert('guardrail_decisions', {'id': uid('guard_'), 'event_id': event_id,
                              'gateway_id': profile['id'] if profile else None,
                              'guardrail_id': step['guardrail_id'], 'name': rail['name'] if rail else 'Unavailable guardrail',
                              'stage': stage, 'outcome': outcome, 'mode': step['mode'], 'on_error': step['on_error'],
                              'latency_ms': (time.perf_counter()-started)*1000, 'created': now()})
            if step['mode'] == 'enforce':
                if outcome == 'blocked' or outcome == 'error' and step['on_error'] == 'block':
                    raise HTTPException(503 if outcome == 'error' else 422,
                                        {'code': 'guardrail_unavailable' if outcome == 'error' else 'guardrail_blocked',
                                         'guardrail': rail['name'] if rail else 'Unavailable guardrail',
                                         'stage': stage, 'request_id': event_id})
                value = changed
        if stage == 'input':
            return {**body, 'messages': value}
        result = copy.deepcopy(response)
        for choice, message in zip(result['choices'], value):
            choice['message'] = message
        return result

    async def evaluate(self, rail, config, stage, messages, body, client):
        texts = []
        map_message_text(messages, lambda value: texts.append(value) or value)
        text = '\n'.join(texts)
        if rail['provider'] == 'builtin':
            if config['rule'] == 'json_schema':
                if stage != 'output':
                    raise ValueError('JSON Schema is an output guardrail')
                try:
                    values=[json.loads(message.get('content') or '') for message in messages]
                except (ValueError, TypeError):
                    return 'blocked', messages
                passed=True
                for value in values:
                    passed=await self.schemas.validate(config['json_schema'],value,config['timeout_seconds']) and passed
                return ('passed' if passed else 'blocked'), messages
            sources = [re.escape(v) for v in config['terms']] if config['rule'] == 'keywords' else [PII[t].pattern for t in config['pii_types']]
            if not sources:return 'passed',messages
            pattern = re.compile('|'.join('(?:'+source+')' for source in sources),re.I)
            matched = pattern.search(text)
            if not matched:
                return 'passed', messages
            if config['action'] == 'block':
                return 'blocked', messages
            remaining=MAX_CHECKED_BYTES-len(encode(messages).encode())
            def replacement(match):
                nonlocal remaining
                remaining-=len('[REDACTED]')-len(match.group().encode())
                if remaining<0:raise ValueError('Checked content exceeds its size limit')
                return '[REDACTED]'
            def redact(value):
                # One pass over the original text: later terms cannot match the
                # replacement marker and expand it repeatedly.
                return pattern.sub(replacement,value)
            return 'redacted', map_message_text(messages, redact)
        endpoint = endpoint_url(config['endpoint'])
        from .secrets import SecretResolver
        secret = await SecretResolver(self.store,self.vault,client).resolve(self.vault.decrypt(rail['secret']))
        headers = {config['auth_header']: ((config['auth_prefix']+' ') if config['auth_prefix'] else '')+secret} if secret else {}
        if rail['provider'] == 'nemo':
            # NeMo's check API makes the decision without generating a second completion.
            conversations = [messages] if stage == 'input' else [body['messages']+[m] for m in messages]
            for conversation in conversations:
                payload = {'messages': conversation, 'guardrails': {'config_id': config['config_id'], 'rail_types': [stage]}}
                data = await self.remote(client, endpoint, payload, headers, config['timeout_seconds'])
                if data.get('status') == 'blocked':
                    return 'blocked', messages
                if data.get('status') != 'passed':
                    # A modification cannot be safely mapped to arbitrary multi-part/tool messages.
                    raise ValueError('NeMo must return a decision-only passed or blocked result')
            return 'passed', messages
        data = await self.remote(client, endpoint, {'protocol': 'pliqra/guardrail-check/1', 'stage': stage, 'messages': messages},
                                 headers, config['timeout_seconds'])
        if not isinstance(data.get('allow'), bool):
            raise ValueError('Webhook must return a boolean allow')
        return ('passed' if data['allow'] else 'blocked'), messages

    async def remote(self, client, endpoint, payload, headers, timeout):
        async with asyncio.timeout(timeout):
            async with client.stream('POST', endpoint, json=payload, headers=headers, timeout=timeout, follow_redirects=False) as response:
                response.raise_for_status()
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 1_000_000:
                        raise ValueError('Guardrail response exceeded 1 MB')
                    chunks.append(chunk)
                data = json.loads(b''.join(chunks))
                if not isinstance(data, dict):
                    raise ValueError('Invalid guardrail result')
                return data
