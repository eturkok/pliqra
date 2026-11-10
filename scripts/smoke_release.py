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
    assert b"Pliqra" in read("/")
    try:
        read("/api/v1/settings")
    except HTTPError as error:
        assert error.code == 401, error.code
    else:
        raise AssertionError("Anonymous management access must be rejected")
    print(json.dumps({"version": version, "health": "ok", "fresh_workspace": True,
                      "anonymous_management": "rejected", "protocol": discovery["protocol"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("version")
    args = parser.parse_args()
    smoke(args.url, args.version)
