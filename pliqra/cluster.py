"""Replica fencing and shared concurrency leases; no inference is silently replayed."""
import asyncio
import json
import psycopg
from contextlib import contextmanager

from fastapi import HTTPException

from .db import now, uid
from .topology import RuntimeTopology

NODE_TIMEOUT=60


class Cluster:
    def __init__(self,store):
        self.store=store
        self.node=uid('node_')
        self.fenced=False
        self.topology=getattr(store,'runtime_topology',None) or RuntimeTopology.from_env()
        with store.transaction():
            self.reap()
            site=store.one('SELECT * FROM sites WHERE id=?',(self.topology.site,))
            if site and site['region']!=self.topology.region:raise RuntimeError('Site region differs from the registered topology')
            if not site:store.insert('sites',{'id':self.topology.site,'region':self.topology.region,'mode':'active','updated':store.clock()})
            store.insert('runtime_nodes',{'id':self.node,'heartbeat':store.clock(),'site':self.topology.site,'region':self.topology.region})
        if store.backend=='postgresql':store.run("SELECT set_config('application_name',?,false)",('pliqra:'+self.topology.site+':'+self.node,))
        store.connection_failure=self.fence

    def fence(self):
        self.fenced=True

    def reap(self):
        store=self.store
        stale=store.all('SELECT id FROM runtime_nodes WHERE heartbeat<?',(store.clock()-NODE_TIMEOUT,))
        for row in stale:
            node=row['id']
            store.run("UPDATE attempts SET outcome='interrupted',finished=? WHERE outcome='pending' AND event_id IN (SELECT id FROM events WHERE node_id=? AND status='pending')",(now(),node))
            store.run("UPDATE gateway_requests SET status='interrupted' WHERE status='pending' AND id IN (SELECT id FROM events WHERE node_id=?)",(node,))
            store.run("UPDATE events SET updated=?,status='interrupted',finished=?,error='Replica lease expired; usage remains reserved for reconciliation' WHERE node_id=? AND status='pending'",(store.clock(),now(),node))
            store.run('DELETE FROM runtime_leases WHERE node_id=?',(node,))
            store.run("UPDATE batch_items SET status='interrupted',error='Replica lease expired; no automatic replay' WHERE status='running' AND batch_id IN (SELECT id FROM batches WHERE node_id=?)",(node,))
            store.run("UPDATE batches SET status='queued',node_id=NULL WHERE node_id=? AND status='running'",(node,))
            store.run("UPDATE evaluations SET status='interrupted',finished=?,error='Replica lease expired; no automatic replay' WHERE node_id=? AND status IN ('queued','running')",(now(),node))
            store.run("UPDATE behavior_schedules SET last_status='interrupted',last_error='Replica lease expired during a check' WHERE id IN (SELECT schedule_id FROM behavior_runs WHERE node_id=? AND status='running')", (node,))
            store.run("UPDATE behavior_runs SET status='interrupted',finished=?,error='Replica lease expired; no automatic replay' WHERE node_id=? AND status='running'", (store.clock(),node))
            store.run('DELETE FROM runtime_nodes WHERE id=?',(node,))

    def ensure_live(self):
        row=self.store.one('SELECT heartbeat FROM runtime_nodes WHERE id=?',(self.node,))
        if self.fenced or not row or row['heartbeat']<self.store.clock()-NODE_TIMEOUT:
            self.fenced=True
            raise HTTPException(503,'Replica lease expired; restart this replica before serving requests')

    def ensure_serving(self):
        self.ensure_live()
        site=self.store.one("SELECT mode,(SELECT value FROM settings WHERE key='maintenance') AS maintenance,"
                            "(SELECT value FROM settings WHERE key='organization_enabled') AS organization_enabled "
                            "FROM sites WHERE id=?",(self.topology.site,))
        if not site or site['mode']!='active':raise HTTPException(503,'Site is draining or on standby; send new requests to another active site',headers={'Retry-After':'5'})
        maintenance=json.loads(site['maintenance']) if site['maintenance'] is not None else False
        enabled=json.loads(site['organization_enabled']) if site['organization_enabled'] is not None else True
        if maintenance:raise HTTPException(503,'Workspace maintenance is in progress')
        if not enabled:raise HTTPException(503,'Organization access is disabled')

    def acquire(self,kind):
        with self.store.transaction():
            if kind=='inference':
                self.ensure_serving()
            else:
                self.ensure_live()
                if not self.store.setting('organization_enabled',True):raise HTTPException(403,'Organization access is disabled')
                if self.store.setting('maintenance',False):raise HTTPException(503,'Workspace maintenance is in progress')
            self.reap()
            management=kind=='management'
            count=self.store.one('SELECT COUNT(*) AS n FROM runtime_leases WHERE kind'+('=' if management else '!=')+"'management'")['n']
            maximum=32 if management else self.store.setting('runtime',{}).get('max_concurrency',64)
            if count>=maximum:
                raise HTTPException(429,'Workspace concurrency limit reached')
            lease=uid('lease_')
            self.store.insert('runtime_leases',{'id':lease,'node_id':self.node,'kind':kind,'created':now()})
            return lease

    def release(self,lease):
        self.store.run('DELETE FROM runtime_leases WHERE id=? AND node_id=?',(lease,self.node))

    @contextmanager
    def checking(self,kind='provider_check'):
        lease=self.acquire(kind)
        try:yield lease
        finally:self.release(lease)

    async def heartbeat(self):
        while True:
            await asyncio.sleep(5)
            try:
                with self.store.transaction():
                    self.ensure_live()
                    self.store.run('UPDATE runtime_nodes SET heartbeat=? WHERE id=?',(self.store.clock(),self.node))
                    self.reap()
            except HTTPException:
                self.fenced=True
                return
            except psycopg.OperationalError:
                # Reconnect on the next heartbeat; a lost/expired lease still fences
                # this process when the authoritative database becomes available.
                continue

    def close(self):
        try:
            with self.store.transaction():
                self.store.run('UPDATE runtime_nodes SET heartbeat=0 WHERE id=?',(self.node,))
                self.reap()
        except psycopg.OperationalError:pass


async def restart_when_fenced(cluster):
    """Opt-in for a process managed by a container/orchestrator restart policy."""
    import os
    import signal
    while not cluster.fenced:await asyncio.sleep(1)
    # A fresh process cannot retain old provider IO or adopt another process's leases.
    os.kill(os.getpid(),signal.SIGTERM)
