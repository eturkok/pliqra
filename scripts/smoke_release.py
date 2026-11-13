"""Check an isolated, empty container; never initialize accounts or call a model."""
import argparse
import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen, Request


def smoke(base: str, version: str) -> None:
    def read(path):
        with urlopen(base.rstrip("/") + path, timeout=5) as response:
            return response.read()

    deadline = time.monotonic() + 60
    while True:
        try:
            health = json.loads(read("/healthz"))
            break
        except (URLError, TimeoutError, ConnectionError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(1)
    assert health == {"status": "ok", "version": version}, health
    read("/livez")
    read("/readyz")
    discovery = json.loads(read("/.well-known/pliqra"))
    assert discovery["protocol"] == "pliqra/0.1-draft", discovery
    if discovery.get('preferred_protocol')=='pliqra/1.0':
        with urlopen(Request(base+'/.well-known/pliqra',headers={'Pliqra-Protocol':'pliqra/1.0'}),timeout=5) as response:
            stable=json.load(response)
            assert stable['protocol']=='pliqra/1.0' and stable['status']=='stable'
            assert response.headers['Pliqra-Protocol']=='pliqra/1.0'

    session = json.loads(read("/api/v1/session"))
    assert session["setup_required"] and session["user"] is None, session
    assert session["version"] == version, session
    console = read("/")
    assert b"Pliqra" in console
    theme_boot = console.index(b'<script src="/static/theme.js"></script>')
    assert theme_boot < console.index(b'rel="stylesheet"'), 'Theme must initialize before styles'
    assert b'data-theme-select' in read('/static/theme.js'), 'Published image is missing theme controls'
    assert b'pliqra.appearance' in read('/static/theme.js'), 'Published image is missing saved appearance'
    assert b'[data-theme="dark"]' in read('/static/theme.css'), 'Published image is missing the dark palette'
    assert b'/static/operations-tools.js' in console
    assert b'Connection diagnosis' in read('/static/operations-tools.js')
    for path in ('/api/v1/diagnostics/runtime', '/api/v1/behavior-checks/missing/impact'):
        try: read(path)
        except HTTPError as error: assert error.code == 401, error.code
        else: raise AssertionError('Anonymous operational access must be rejected')
    try:
        read("/api/v1/settings")
    except HTTPError as error:
        assert error.code == 401, error.code
    else:
        raise AssertionError("Anonymous management access must be rejected")
    print(json.dumps({"version": version, "health": "ok", "fresh_workspace": True,
                      "anonymous_management": "rejected", "protocol": discovery["protocol"],
                      "console_theme_assets": "verified", "operational_assets_and_anonymous_boundaries": "verified"}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("version")
    args = parser.parse_args()
    smoke(args.url, args.version)
