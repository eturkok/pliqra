"""PostgreSQL storage with serialized workspace writes and explicit schema isolation."""
import hashlib
import re
import sqlite3
import threading
from pathlib import Path
from contextlib import contextmanager
from .telemetry import traced

import psycopg
from psycopg.rows import dict_row
from psycopg.pq import TransactionStatus

from .db import SQLiteStore, SCHEMA_VERSION, now
from .storage_schema import BASE_SCHEMA


def placeholders(statement):
    # Application SQL is static. Bind markers inside SQL string literals stay literal.
    pieces=re.split(r"('(?:[^']|'')*')",statement)
    return ''.join(part if index%2 else part.replace('?', '%s') for index,part in enumerate(pieces))


def pg_definition(statement):
    return re.sub(r'\bREAL\b','DOUBLE PRECISION',re.sub(r'\bINTEGER\b','BIGINT',statement))


class PostgresStore(SQLiteStore):
    backend='postgresql'

    def __init__(self,directory,url,schema='pliqra'):
        if not re.fullmatch(r'[a-z][a-z0-9_]{0,62}',schema):
            raise ValueError('Invalid database schema name')
        self.directory=Path(directory)
        self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.schema=schema
        options=psycopg.conninfo.conninfo_to_dict(url)
        if options.get('target_session_attrs','read-write')!='read-write':raise ValueError('The control database must use target_session_attrs=read-write')
        self.database_url=psycopg.conninfo.make_conninfo(url,target_session_attrs='read-write')
        self.lock=threading.RLock()
        self.conn=psycopg.connect(self.database_url,autocommit=True,row_factory=dict_row,connect_timeout=5)
        self.lock_id=int.from_bytes(hashlib.sha256(('pliqra:'+schema).encode()).digest()[:8],signed=True)
        self.conn.execute('SET lock_timeout TO \'10s\'')
        self.conn.execute('SET statement_timeout TO \'30s\'')
        # Schema identifiers are checked above, never supplied as arbitrary SQL.
        self.conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        self.conn.execute(f'SET search_path TO "{schema}"')
        try:
            with self.transaction():
                self.conn.execute('CREATE TABLE IF NOT EXISTS schema_metadata (version BIGINT NOT NULL)')
                row=self.conn.execute('SELECT version FROM schema_metadata').fetchone()
                if row and row['version']>SCHEMA_VERSION:
                    raise RuntimeError('Workspace requires a newer Pliqra version')
                for statement in BASE_SCHEMA.split(';'):
                    if statement.strip() and not statement.strip().startswith('PRAGMA'):
                        self.conn.execute(pg_definition(statement))
            self.migrate()
            with self.transaction():
                if not self.one('SELECT id FROM projects LIMIT 1'):
                    self.run('INSERT INTO projects (id,name,monthly_budget,created) VALUES (?,?,?,?)',('default','Default workspace',None,now()))
        except BaseException:
            self.conn.close()
            raise

    def columns(self,table):
        return [r['column_name'] for r in self.all('SELECT column_name FROM information_schema.columns WHERE table_schema=? AND table_name=?',(self.schema,table))]

    def tables(self):
        return [r['table_name'] for r in self.all('SELECT table_name FROM information_schema.tables WHERE table_schema=? AND table_type=\'BASE TABLE\'',(self.schema,))]

    def primary_key(self,table):
        return [r['column_name'] for r in self.all("SELECT k.column_name FROM information_schema.table_constraints t JOIN information_schema.key_column_usage k ON k.constraint_schema=t.constraint_schema AND k.constraint_name=t.constraint_name AND k.table_name=t.table_name WHERE t.constraint_type='PRIMARY KEY' AND t.table_schema=? AND t.table_name=? ORDER BY k.ordinal_position",(self.schema,table))]

    def set_schema_version(self,value):
        self.conn.execute('DELETE FROM schema_metadata')
        self.conn.execute('INSERT INTO schema_metadata VALUES (%s)',(value,))

    @traced("database.query")
    def all(self,sql,args=()):
        with self.lock:
            self.ensure_connection()
            return self._execute(sql,args).fetchall()

    @traced("database.query")
    def run(self,sql,args=()):
        with self.lock:
            self.ensure_connection()
            return self._execute(sql,args)

    def ensure_connection(self):
        if self.conn.closed or self.conn.broken:
            self.conn=psycopg.connect(self.database_url,autocommit=True,row_factory=dict_row,connect_timeout=5)
            self.conn.execute(f'SET search_path TO "{self.schema}"')
            self.conn.execute("SET lock_timeout TO '10s'")
            self.conn.execute("SET statement_timeout TO '30s'")

    def _execute(self,sql,args=()):
        if self.conn.info.transaction_status==TransactionStatus.IDLE and re.match(r'\s*(INSERT|UPDATE|DELETE|ALTER|CREATE|DROP|TRUNCATE)\b',sql,re.I):
            with self.transaction():return self._execute(sql,args)
        try:
            return self.conn.execute(placeholders(pg_definition(sql)),args or None)
        except psycopg.IntegrityError as exc:
            raise sqlite3.IntegrityError('Database constraint violation') from exc
        except psycopg.OperationalError:
            self.conn.close()
            if hasattr(self,'connection_failure'):self.connection_failure()
            raise

    @contextmanager
    def transaction(self):
        # A single workspace lock keeps budgets, rolling quotas and configuration
        # publication atomic across processes. Different organization schemas are independent.
        with self.lock:
            self.ensure_connection()
            try:
                with self.conn.transaction():
                    row=self.conn.execute("SELECT pg_advisory_xact_lock(%s),current_setting('transaction_read_only') AS read_only",(self.lock_id,)).fetchone()
                    if row['read_only']=='on':raise psycopg.OperationalError('The control database is read-only')
                    yield self
            except psycopg.OperationalError:
                self.conn.close()
                if hasattr(self,'connection_failure'):self.connection_failure()
                raise

    def ensure_writable(self):
        if self.one("SELECT current_setting('transaction_read_only') AS value")['value']=='on':
            if hasattr(self,'connection_failure'):self.connection_failure()
            from fastapi import HTTPException
            raise HTTPException(503,'The control database is read-only; restore a single writable authority')

    def database_size(self):
        return self.one('SELECT COALESCE(SUM(pg_total_relation_size(c.oid)),0)::BIGINT AS bytes FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=?',(self.schema,))['bytes']

    def clock(self):return float(self.one('SELECT EXTRACT(EPOCH FROM clock_timestamp()) AS instant')['instant'])

    def close(self):
        self.conn.close()
