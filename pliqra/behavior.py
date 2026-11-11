"""Opt-in, bounded observation of model behavior on an existing deployment."""
import asyncio
import json
import logging
from typing import Annotated

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .access import current_principal
from .db import encode, uid
from .evaluation import Probe, capability_probe


class Check(Probe):
    model_config = ConfigDict(extra='forbid')
    name: Annotated[str, Field(min_length=1, max_length=100)]
    prompt: Annotated[str, Field(max_length=4000)] = ''
    contains: Annotated[str, Field(max_length=500)] = ''
    max_latency_ms: Annotated[int | None, Field(ge=1, le=90000)] = None

    @model_validator(mode='after')
    def text_assertions(self):
        if self.capability != 'chat' and (self.prompt or self.contains):
            raise ValueError('Custom prompts and expected text apply to chat checks')
        return self


class Schedule(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: Annotated[str, Field(min_length=1, max_length=100)]
    deployment_id: str
    project_id: str = 'default'
    enabled: bool = False
    interval_seconds: Annotated[int, Field(ge=60, le=604800)] = 3600
    max_runs_per_day: Annotated[int, Field(ge=1, le=288)] = 24
    max_tokens: Annotated[int, Field(ge=16, le=512)] = 128
    timeout_seconds: Annotated[int, Field(ge=5, le=90)] = 30
    checks: Annotated[list[Check], Field(min_length=1, max_length=8)]

    @model_validator(mode='after')
    def unique_checks(self):
        if len({check.name for check in self.checks}) != len(self.checks):
            raise ValueError('Check names must be unique in a schedule')
        return self


def endpoint_fingerprint(gateway, deployment_id, project_id):
    port = {'id': 'probe:' + deployment_id, 'name': 'Capability probe', 'version': 1, 'strategy': 'priority',
            'deployments': [deployment_id], 'requirements': [], 'enabled': True, 'cache_ttl': 0,
            'project_id': project_id}
    return gateway.fingerprint(port)


def owner(store, actor):
    principal = current_principal(store, {'id': actor, 'kind': 'user', 'role': 'admin'})
    if principal['role'] != 'admin':
        raise HTTPException(403, 'The schedule owner must remain an active administrator')
    return principal


def claim(app, record_id=None, initiated_by=None):
    """Reserve a complete cycle once across all replicas, before provider IO."""
    store, gateway = app.state.store, app.state.gateway
    with store.transaction():
        gateway.cluster.ensure_serving()
        gateway.cluster.reap()
        if store.one("SELECT id FROM behavior_runs WHERE status='running' LIMIT 1"):
            if record_id:
                raise HTTPException(409, 'A behavior-check cycle is already running')
            return None
        instant = store.clock()
        if record_id:
            row = store.one('SELECT * FROM behavior_schedules WHERE id=?', (record_id,))
            if not row:
                raise HTTPException(404, 'Behavior-check schedule not found')
        else:
            location = " AND (c.site IS NULL OR c.site='' OR c.site=?)" if gateway.cluster.topology.multi_dc else ''
            args = [instant, gateway.cluster.topology.site] if location else [instant]
            row = store.one('SELECT b.* FROM behavior_schedules b LEFT JOIN deployments d ON d.id=b.deployment_id '
                            'LEFT JOIN connections c ON c.id=d.connection_id WHERE b.enabled=1 AND b.next_run<=?' +
                            location + ' ORDER BY b.next_run,b.id LIMIT 1', args)
            if not row:
                return None
        config = Schedule.model_validate_json(app.state.vault.decrypt(row['config']))
        connection = store.one('SELECT c.site FROM connections c JOIN deployments d ON d.connection_id=c.id WHERE d.id=?',
                               (row['deployment_id'],))
        if gateway.cluster.topology.multi_dc and connection and connection['site'] not in ('', gateway.cluster.topology.site):
            raise HTTPException(409, 'Run this check from the model endpoint site')
        try:
            owner(store, row['actor'])
        except HTTPException:
            if record_id:
                raise
            store.update('behavior_schedules', row['id'], {'last_status': 'error',
                         'last_error': 'Schedule owner is no longer an active administrator',
                         'next_run': instant + config.interval_seconds})
            return None
        day = int(instant // 86400) * 86400
        used = row['daily_runs'] if row['day_start'] == day else 0
        if used >= config.max_runs_per_day:
            store.update('behavior_schedules', row['id'], {'next_run': day + 86400})
            if record_id:
                raise HTTPException(429, 'Daily behavior-check run limit reached')
            return None
        fingerprint = endpoint_fingerprint(gateway, row['deployment_id'], config.project_id)
        record = {'id': uid('behavior_'), 'schedule_id': row['id'], 'version': row['version'],
                  'node_id': gateway.cluster.node, 'generation': gateway.generation, 'status': 'running',
                  'created': instant, 'config': row['config'], 'fingerprint': fingerprint, 'results': '[]'}
        store.insert('behavior_runs', record)
        reset = row['last_fingerprint'] != fingerprint
        store.update('behavior_schedules', row['id'], {'day_start': day, 'daily_runs': used + 1,
                     'next_run': instant + config.interval_seconds, 'last_run_at': instant,
                     'last_status': 'running', 'last_error': None,
                     **({'last_results': '[]', 'last_success_at': None} if reset else {})})
        store.audit(initiated_by or row['actor'], 'behavior.started', row['id'], {'run_id': record['id'], 'manual': bool(record_id), 'execution_owner': row['actor']})
        return {**record, 'actor': row['actor']}


def finish(app, run, status, results, error=None):
    store, gateway = app.state.store, app.state.gateway
    with store.transaction():
        gateway.cluster.ensure_live()
        saved = store.one('SELECT status,node_id FROM behavior_runs WHERE id=?', (run['id'],))
        if not saved or saved['status'] != 'running' or saved['node_id'] != gateway.cluster.node:
            return
        if gateway.generation != run['generation']:
            return
        instant = store.clock()
        current = store.one('SELECT * FROM behavior_schedules WHERE id=?', (run['schedule_id'],))
        if not current or current['version'] != run['version']:
            status, error = 'superseded', 'Schedule changed during the cycle'
        store.update('behavior_runs', run['id'], {'status': status, 'results': encode(results),
                     'finished': instant, 'error': error})
        if not current or current['version'] != run['version']:
            return
        updates = {'last_status': status, 'last_error': error}
        if status in ('passed', 'failed', 'error') and results:
            old = {item['name']: item for item in json.loads(current['last_results'])} if current['last_fingerprint'] == run['fingerprint'] else {}
            for result in results:
                previous = old.get(result['name'], {})
                result['last_success_at'] = result['tested_at'] if result['passed'] else previous.get('last_success_at')
                prior = previous.get('outcome')
                changed = prior is not None and prior != result['outcome']
                result['changed'] = changed
                result['last_change_at'] = result['tested_at'] if changed else previous.get('last_change_at')
                result['previous_outcome'] = prior if changed else previous.get('previous_outcome')
                if changed or prior is None and not result['passed']:
                    transition = 'recovered' if result['passed'] else 'failed' if result['outcome'] == 'failed' else 'error'
                    message = result['name'] + ': ' + (prior or 'not tested') + ' → ' + result['outcome']
                    store.insert('notifications', {'id': uid('note_'), 'kind': 'model-behavior',
                                 'target': current['id'], 'message': current['name'] + ' · ' + message, 'created': instant})
                    store.audit(run['actor'], 'behavior.' + transition, current['id'], {'run_id': run['id'], 'check': result['name']})
            merged = {**old, **{result['name']: result for result in results}}
            updates.update(last_results=encode(list(merged.values())), last_fingerprint=run['fingerprint'])
            if status == 'passed':
                updates['last_success_at'] = instant
            store.update('behavior_runs', run['id'], {'results': encode(results)})
        store.update('behavior_schedules', current['id'], updates)


async def execute(app, run):
    store, gateway = app.state.store, app.state.gateway
    config = Schedule.model_validate_json(app.state.vault.decrypt(run['config']))
    results = []
    try:
        for check in config.checks:
            gateway.cluster.ensure_serving()
            ownership = store.one('SELECT status,node_id FROM behavior_runs WHERE id=?', (run['id'],))
            if not ownership or ownership['status'] != 'running' or ownership['node_id'] != gateway.cluster.node:
                return
            current = store.one('SELECT version FROM behavior_schedules WHERE id=?', (run['schedule_id'],))
            if not current or current['version'] != run['version'] or gateway.generation != run['generation']:
                finish(app, run, 'superseded', results, 'Schedule or workspace changed during the cycle')
                return
            if endpoint_fingerprint(gateway, config.deployment_id, config.project_id) != run['fingerprint']:
                finish(app, run, 'superseded', results, 'Endpoint configuration changed during the cycle')
                return
            principal = owner(store, run['actor'])
            try:
                async with asyncio.timeout(config.timeout_seconds):
                    result = await capability_probe(app, config.deployment_id, check, principal, observe_only=True,
                                                    check=check, max_tokens=config.max_tokens, project_id=config.project_id)
            except TimeoutError:
                result = {'passed': False, 'outcome': 'error', 'detail': 'Behavior check timed out', 'request_id': None, 'checks': []}
            results.append({**result, 'name': check.name, 'tested_at': store.clock()})
            with store.transaction():
                gateway.cluster.ensure_live()
                ownership = store.one('SELECT status,node_id FROM behavior_runs WHERE id=?', (run['id'],))
                if gateway.generation != run['generation'] or not ownership or ownership['status'] != 'running' or ownership['node_id'] != gateway.cluster.node:
                    return
                store.update('behavior_runs', run['id'], {'results': encode(results)})
        status = 'error' if any(result['outcome'] == 'error' for result in results) else 'failed' if any(not result['passed'] for result in results) else 'passed'
        finish(app, run, status, results)
    except asyncio.CancelledError:
        if not gateway.cluster.fenced:
            finish(app, run, 'interrupted', results, 'Worker stopped during a cycle; started calls are not replayed')
        raise
    except HTTPException as exc:
        if not gateway.cluster.fenced:
            changed = gateway.generation != run['generation'] or endpoint_fingerprint(gateway, config.deployment_id, config.project_id) != run['fingerprint']
            finish(app, run, 'superseded' if changed else 'error', results,
                   'Check stopped with HTTP ' + str(exc.status_code))
    except Exception:
        if not gateway.cluster.fenced:
            finish(app, run, 'error', results, 'Check stopped unexpectedly; inspect request accounting')


def launch(app, run):
    task = asyncio.create_task(execute(app, run))
    app.state.jobs.add(task)
    task.add_done_callback(app.state.jobs.discard)
    return task


async def worker(app):
    while True:
        await asyncio.sleep(5)
        try:
            run = claim(app)
            if run:
                launch(app, run)
        except HTTPException:
            continue
        except Exception:
            # Database loss fences the node. A fresh process can take a later cycle.
            if app.state.gateway.cluster.fenced:
                return
            logging.getLogger(__name__).exception('Behavior-check scheduler failed; retrying on the next tick')


def mount(app):
    ctx = app.state.context
    store, vault, admin = ctx['store'], ctx['vault'], ctx['admin']

    def public(row):
        config = json.loads(vault.decrypt(row['config']))
        deployment = store.one('SELECT d.model,c.name AS connection,c.endpoint FROM deployments d JOIN connections c ON c.id=d.connection_id WHERE d.id=?', (row['deployment_id'],))
        instant = store.clock()
        return {**{key: value for key, value in row.items() if key not in ('config', 'last_results')},
                **config, 'deployment': deployment, 'last_results': json.loads(row['last_results']),
                'daily_runs': row['daily_runs'] if row['day_start'] == int(instant // 86400) * 86400 else 0,
                'enabled': bool(row['enabled']),
                'maximum_daily_requests': config['max_runs_per_day'] * len(config['checks'])}

    @app.get('/api/v1/behavior-checks', tags=['Behavior checks'])
    def schedules(p=Depends(admin)):
        rows = store.all('SELECT * FROM behavior_schedules ORDER BY created DESC,id')
        return {'items': [public(row) for row in rows]}

    def save(body, p, record_id=None):
        if not store.one('SELECT id FROM deployments WHERE id=?', (body.deployment_id,)):
            raise HTTPException(422, 'Select an existing model deployment')
        if not store.one('SELECT id FROM projects WHERE id=?', (body.project_id,)):
            raise HTTPException(422, 'Select an existing project for accounting and budget enforcement')
        with store.transaction():
            previous = store.one('SELECT * FROM behavior_schedules WHERE id=?', (record_id,)) if record_id else None
            if record_id and not previous:
                raise HTTPException(404, 'Behavior-check schedule not found')
            if not record_id and store.one('SELECT COUNT(*) AS n FROM behavior_schedules')['n'] >= 100:
                raise HTTPException(409, 'This workspace already has 100 behavior-check schedules')
            record_id = record_id or uid('watch_')
            instant = store.clock()
            config = body.model_dump()
            changed = previous and {k:v for k,v in json.loads(vault.decrypt(previous['config'])).items() if k not in ('enabled','name','interval_seconds','max_runs_per_day')} != {k:v for k,v in config.items() if k not in ('enabled','name','interval_seconds','max_runs_per_day')}
            fields = {'name': body.name, 'deployment_id': body.deployment_id, 'actor': p['id'], 'enabled': int(body.enabled),
                      'config': vault.encrypt(encode(config)), 'version': previous['version'] + 1 if previous else 1,
                      'next_run': instant + body.interval_seconds, 'updated': instant}
            if previous:
                fields['last_status'] = 'pending' if body.enabled else 'paused'
                if changed:
                    fields.update(last_results='[]', last_fingerprint=None, last_success_at=None)
                store.update('behavior_schedules', record_id, fields)
            else:
                store.insert('behavior_schedules', {'id': record_id, **fields, 'created': instant})
            store.audit(p['id'], 'behavior.configured', record_id, {'enabled': body.enabled, 'checks': len(body.checks)})
        return public(store.one('SELECT * FROM behavior_schedules WHERE id=?', (record_id,)))

    @app.post('/api/v1/behavior-checks', tags=['Behavior checks'], status_code=201)
    def create(body: Schedule, p=Depends(admin)):
        return save(body, p)

    @app.put('/api/v1/behavior-checks/{record_id}', tags=['Behavior checks'])
    def update(record_id: str, body: Schedule, p=Depends(admin)):
        return save(body, p, record_id)

    @app.post('/api/v1/behavior-checks/{record_id}/run', tags=['Behavior checks'], status_code=202)
    async def run_now(record_id: str, p=Depends(admin)):
        run = claim(app, record_id, p['id'])
        launch(app, run)
        return {'id': run['id']}

    @app.get('/api/v1/behavior-checks/{record_id}/runs', tags=['Behavior checks'])
    def history(record_id: str, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), p=Depends(admin)):
        if not store.one('SELECT id FROM behavior_schedules WHERE id=?', (record_id,)):
            raise HTTPException(404, 'Behavior-check schedule not found')
        rows = store.all('SELECT id,version,node_id,status,created,finished,fingerprint,results,error FROM behavior_runs WHERE schedule_id=? ORDER BY created DESC,id LIMIT ? OFFSET ?', (record_id, limit, offset))
        return {'items': [{**row, 'results': json.loads(row['results'])} for row in rows],
                'total': store.one('SELECT COUNT(*) AS n FROM behavior_runs WHERE schedule_id=?', (record_id,))['n']}
