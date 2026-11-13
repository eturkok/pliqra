"""Shared, bounded sign-in throttling with explicit trusted proxy handling."""
import hashlib
import hmac
import ipaddress
import math

from fastapi import HTTPException
from .config import env
from .db import uid


class LoginControl:
    window = 300

    def __init__(self, store, vault):
        self.store, self.vault = store, vault
        values = env('PLIQRA_TRUSTED_PROXIES', '').split(',')
        if len(values) > 128:
            raise ValueError('At most 128 trusted proxy networks are supported')
        self.trusted = [ipaddress.ip_network(value.strip(), strict=False) for value in values if value.strip()]

    def address(self, request):
        peer = request.client.host if request.client else 'unknown'
        try: peer_ip = ipaddress.ip_address(peer)
        except ValueError: return peer
        def trusted(ip):
            return any(ip.version == network.version and ip in network for network in self.trusted)
        if not trusted(peer_ip):
            return str(peer_ip)
        values = request.headers.getlist('x-forwarded-for')
        if not values:
            return str(peer_ip)
        if len(values) != 1 or len(values[0]) > 4096 or len(values[0].split(',')) > 20:
            raise HTTPException(400, 'Invalid forwarded client address')
        try: chain = [ipaddress.ip_address(value.strip()) for value in values[0].split(',')] + [peer_ip]
        except ValueError: raise HTTPException(400, 'Invalid forwarded client address') from None
        while len(chain) > 1 and trusted(chain[-1]):
            chain.pop()
        return str(chain[-1])

    def check(self, request):
        address = self.address(request)
        bucket = 'login:' + hmac.new(self.vault.key, address.encode(), hashlib.sha256).hexdigest()
        with self.store.transaction():
            instant = self.store.clock()
            self.store.run('DELETE FROM auth_attempts WHERE created<=?', (instant - self.window,))
            attempts = self.store.one('SELECT COUNT(*) AS n,MIN(created) AS oldest FROM auth_attempts WHERE bucket=? AND created>?', (bucket, instant - self.window))
            if attempts['n'] >= 15:
                retry = max(1, math.ceil(attempts['oldest'] + self.window - instant))
                raise HTTPException(429, 'Too many sign-in attempts. Try again later.', headers={'Retry-After': str(retry)})
            if self.store.one('SELECT COUNT(*) AS n FROM auth_attempts')['n'] >= 100000:
                raise HTTPException(429, 'Sign-in protection is temporarily at capacity.', headers={'Retry-After': '60'})
            self.store.insert('auth_attempts', {'id': uid('login_'), 'bucket': bucket, 'created': instant})
