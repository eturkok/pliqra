from .config import env
import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
import json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from cryptography.fernet import Fernet
from fastapi import HTTPException

ENCRYPTED_COLUMNS=(('connections','secret'),('identity_providers','secret'),('guardrails','secret'),
                   ('secret_providers','secret'),('quality_judges','secret'),('analytics_targets','secret'),('fleet_members','secret'),('batch_items','request'),('batch_items','result'),('cache_entries','payload'),('event_content','payload'))


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def bearer_token(authorization):
    scheme, separator, token = authorization.partition(' ')
    return token.strip() if separator and scheme.lower()=='bearer' and token.strip() else None


def hash_password(password):
    salt = secrets.token_bytes(16)
    value = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return base64.urlsafe_b64encode(salt + value).decode()


def verify_password(password, stored):
    try:
        raw = base64.urlsafe_b64decode(stored)
        value = hashlib.scrypt(password.encode(), salt=raw[:16], n=16384, r=8, p=1)
        return hmac.compare_digest(value, raw[16:])
    except (ValueError, TypeError):
        return False


def encrypted_values(connection):
    """Stream every registered encrypted source, including optional older-schema tables."""
    tables={r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table,column in ENCRYPTED_COLUMNS:
        if table in tables:
            if column not in {r[1] for r in connection.execute(f'PRAGMA table_info({table})')}:continue
            for row in connection.execute(f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL AND {column}!=''"):
                yield row[0]
    if 'identity_providers' in tables:
        for row in connection.execute('SELECT config FROM identity_providers'):
            value=json.loads(row[0]).get('sp_private_key')
            if value:
                yield value


def validate_encryption(connection, key):
    cipher=Fernet(key)
    for value in encrypted_values(connection):
        cipher.decrypt(value.encode())


class Vault:
    def __init__(self, directory: Path, store=None, key=None):
        key = key or env("PLIQRA_ENCRYPTION_KEY")
        key_file=env('PLIQRA_ENCRYPTION_KEY_FILE')
        if not key and key_file:
            key=Path(key_file).read_bytes().strip()
        self.external=bool(key)
        if store is not None and store.backend=='postgresql' and not key:
            raise RuntimeError('PostgreSQL replicas require the same explicit PLIQRA_ENCRYPTION_KEY')
        path = directory / "encryption.key"
        if not key:
            if not path.exists():
                database=directory/'pliqra.sqlite3'
                if database.exists():
                    connection=sqlite3.connect(database)
                    try:
                        stored=next(encrypted_values(connection),None)
                    finally:connection.close()
                    if stored:raise RuntimeError('The encryption key is missing. Restore the original encryption.key or an encrypted backup.')
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(Fernet.generate_key())
            key = path.read_bytes()
        self.fernet = Fernet(key)
        raw_key = key.encode() if isinstance(key, str) else key
        self.key = raw_key
        self.cookie_secret = hmac.new(raw_key, b"pliqra-cookie-signing", hashlib.sha256).hexdigest()
        if store is not None and store.backend=='postgresql':
            try:
                for table,column in ENCRYPTED_COLUMNS:
                    after=''
                    while True:
                        limit=1 if table=='batch_items' else 100
                        rows=store.all(f"SELECT id,{column} FROM {table} WHERE id>? ORDER BY id LIMIT {limit}",(after,))
                        if not rows:break
                        for row in rows:
                            if row[column]:self.fernet.decrypt(row[column].encode())
                        after=rows[-1]['id']
                for row in store.all('SELECT config FROM identity_providers'):
                    private=json.loads(row['config']).get('sp_private_key')
                    if private:self.fernet.decrypt(private.encode())
            except Exception as exc:
                raise RuntimeError('The encryption key does not match stored credentials') from exc
        database=directory/'pliqra.sqlite3'
        if database.exists():
            connection=sqlite3.connect(database)
            try:
                validate_encryption(connection,raw_key)
            except Exception as exc:
                raise RuntimeError('The encryption key does not match stored credentials. Restore the original key or encrypted backup.') from exc
            finally:connection.close()

    def encrypt(self, value):
        return self.fernet.encrypt(value.encode()).decode() if value else ""

    def decrypt(self, value):
        return self.fernet.decrypt(value.encode()).decode() if value else ""

    def session_secret(self):
        # Separate signing material from the encryption key, stable across restarts.
        return self.cookie_secret

    def replace_key(self, key):
        self.fernet = Fernet(key)
        self.key = key
        self.cookie_secret = hmac.new(key, b"pliqra-cookie-signing", hashlib.sha256).hexdigest()


def endpoint_url(value):
    parts = urlsplit(value.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise HTTPException(422, "Enter an http:// or https:// endpoint")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise HTTPException(422, "Put credentials in the API key field; endpoint URLs cannot contain credentials, queries or fragments")
    try:
        parts.port
    except ValueError:
        raise HTTPException(422, "Invalid endpoint port")
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))
