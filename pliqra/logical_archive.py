"""Transactional, encrypted logical snapshots for shared PostgreSQL workspaces."""
import base64
import hashlib
import json
import re
import secrets

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException

from .db import SCHEMA_VERSION, encode, now
from .security import ENCRYPTED_COLUMNS

EXCLUDED={'runtime_nodes','runtime_leases','schema_metadata','deployment_health','connection_health'}


def archive_key(password,salt):
    return base64.urlsafe_b64encode(hashlib.pbkdf2_hmac('sha256',password.encode(),salt,600000))


def backup(store,vault,password):
    with store.transaction():
        tables={};size=0
        for table in store.tables():
            if table in EXCLUDED or not re.fullmatch('[a-z_]+',table):continue
            keys=store.primary_key(table)
            if not keys:raise HTTPException(422,'Workspace table has no stable backup key')
            tables[table]=[];after=None
            while True:
                predicate=(' WHERE ('+','.join(keys)+')>('+','.join('?' for _ in keys)+')') if after is not None else ''
                # Batch responses can each contain megabytes; never fetch a large page of them.
                rows=store.all(f'SELECT * FROM "{table}"'+predicate+' ORDER BY '+','.join(keys)+' LIMIT '+('1' if table=='batch_items' else '100'),after or ())
                if not rows:break
                for row in rows:
                    size+=len(encode(row).encode())
                    if size>32_000_000:raise HTTPException(413,'Use a native database backup for workspaces larger than the 32 MB portable archive limit')
                    tables[table].append(row)
                after=tuple(rows[-1][key] for key in keys)
        document={'format':'pliqra-logical/1','schema':SCHEMA_VERSION,'created':now(),'tables':tables,'key':vault.key.decode()}
        payload=encode(document).encode()
        if len(payload)>32_000_000:raise HTTPException(413,'Portable archive exceeds 32 MB; use native database backup')
        salt=secrets.token_bytes(16)
        return b'PLQLOGI01'+salt+Fernet(archive_key(password,salt)).encrypt(payload)


def restore(store,vault,archive,password):
    try:
        data=json.loads(Fernet(archive_key(password,archive[9:25])).decrypt(archive[25:]))
        if data['format']!='pliqra-logical/1' or data['schema'] not in (6, SCHEMA_VERSION) or data['key'].encode()!=vault.key:
            raise ValueError()
        tables=data['tables']
        allowed=set(store.tables())-EXCLUDED
        if data['schema'] == 6 and SCHEMA_VERSION == 7:
            new_tables = {'behavior_schedules', 'behavior_runs'}
            if allowed - set(tables) != new_tables:
                raise ValueError()
            tables.update({table: [] for table in new_tables})
        if set(tables)!=allowed or any(not re.fullmatch('[a-z_]+',t) for t in tables):raise ValueError()
        if not any(r['role']=='admin' and r['issuer']=='local' and r['active'] for r in tables['users']):raise ValueError()
        for table,column in ENCRYPTED_COLUMNS:
            for row in tables.get(table,[]):
                if row.get(column):vault.decrypt(row[column])
        for row in tables['identity_providers']:
            private=json.loads(row['config']).get('sp_private_key')
            if private:vault.decrypt(private)
        for table,rows in tables.items():
            columns=set(store.columns(table))
            if not isinstance(rows,list) or any(set(row)!=columns for row in rows):raise ValueError()
    except (InvalidToken,ValueError,KeyError,TypeError,AttributeError):
        raise HTTPException(422,'Logical backup is invalid, incompatible or uses a different workspace encryption key')
    if store.backend!='postgresql':raise HTTPException(422,'Restore this logical archive into PostgreSQL')
    with store.transaction():
        # All tables belong to this explicitly selected organization schema. PostgreSQL
        # rolls back both TRUNCATE and inserts on any constraint/validation failure.
        store.run('TRUNCATE '+','.join('"'+t+'"' for t in tables))
        remaining=dict(tables)
        # Defer foreign-key checks by inserting tables in dependency order.
        while remaining:
            progressed=False
            for table in list(remaining):
                references=store.all('SELECT DISTINCT ccu.table_name FROM information_schema.table_constraints tc JOIN information_schema.constraint_column_usage ccu ON tc.constraint_name=ccu.constraint_name AND tc.constraint_schema=ccu.constraint_schema WHERE tc.constraint_type=\'FOREIGN KEY\' AND tc.table_schema=? AND tc.table_name=?',(store.schema,table))
                if any(r['table_name'] in remaining and r['table_name']!=table for r in references):continue
                for row in remaining.pop(table):store.insert(table,row)
                progressed=True
            if not progressed:raise HTTPException(422,'Archive has an unsupported dependency cycle')
        store.run('DELETE FROM sessions')
        store.run('DELETE FROM saml_flows')
        store.run('DELETE FROM saml_assertions')
        store.run("UPDATE events SET updated=?,status='interrupted',finished=?,error='Restored during execution; reconcile usage' WHERE status='pending'",(store.clock(),now(),))
        store.run("UPDATE attempts SET outcome='interrupted',finished=? WHERE outcome='pending'",(now(),))
        store.run("UPDATE gateway_requests SET status='interrupted' WHERE status='pending'")
        store.run("UPDATE evaluations SET status='interrupted',finished=?,error='Restored during execution' WHERE status IN ('queued','running')",(now(),))
        store.run("UPDATE batch_items SET status='interrupted',error='Restored during execution; no automatic replay' WHERE status='running'")
        store.run("UPDATE batches SET status='queued',node_id=NULL WHERE status='running'")
        store.run("UPDATE behavior_runs SET status='interrupted',finished=?,error='Restored during execution; no automatic replay' WHERE status='running'", (store.clock(),))
        store.run("UPDATE behavior_schedules SET enabled=0,last_status='paused',last_error='Re-enable scheduled checks after reviewing the restored workspace'")
        store.set_setting('maintenance',True)
    return {'restored':True,'sessions_invalidated':True}
