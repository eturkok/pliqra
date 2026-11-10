# Get started with Pliqra

[← Pliqra](../README.md)

Run commands from the repository root. Bring an already running OpenAI-compatible or Ollama model endpoint.

## Run a prebuilt image

For the published AMD64/ARM64 release candidate, follow [the prebuilt-image installation](DISTRIBUTION.md#install-the-release-candidate). It requires no source checkout or local build. The commands below build from source instead.

## Build with Docker

Prerequisites: Docker with Compose and an already running model endpoint.

```sh
git clone https://github.com/eturkok/pliqra.git
cd pliqra
docker compose up --build -d
```

Open **http://localhost:8088**, create your administrator account, and connect a model endpoint. No default password, demo account or sample traffic is shipped.

Follow the three cards on **Overview**. Progress comes from retained successful inference records and current, unexpired bindings, independently of reporting filters. The contract panel separates declared capabilities from observed verification. No inference or paid probe runs automatically during onboarding.

Prefer a single Docker command sequence?

```sh
docker build -t pliqra:2.2.0 .
docker run -d --name pliqra \
  -p 127.0.0.1:8088:8080 \
  -v pliqra-data:/data \
  --add-host host.docker.internal:host-gateway \
  --restart unless-stopped \
  pliqra:2.2.0
```

Open http://localhost:8088, create your administrator, then **Connect model**. Supply a running OpenAI-compatible or Ollama endpoint. Models become stable named AI services automatically. A manual model name handles servers without model-list discovery. No accounts, credentials, or sample traffic are seeded.

Copy `.env.example` to `.env` for optional Compose settings. `PLIQRA_HTTP_PORT` changes its default host port of 8088. The Compose deployment persists `pliqra-data`; the standalone command above uses the same volume name. To stop Compose use `docker compose down`; preserve the volume to keep the workspace.

The endpoint must be reachable **from inside the container**. Use `http://host.docker.internal:PORT` for a host service, or a container hostname on a shared Docker network. `localhost` inside Pliqra is Pliqra itself.

## Call a service

Create an access key in **Projects & keys**. Its complete value is shown once. Use an **AI service name**, not the underlying deployment name:

```sh
export PLIQRA_API_KEY='your-key'
curl http://localhost:8088/v1/chat/completions \
  -H "Authorization: Bearer $PLIQRA_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"your-service","messages":[{"role":"user","content":"Hello"}],"stream":true}'
```

`X-Request-ID` links to the corresponding request record; `X-Pliqra-Model` identifies the selected deployment model. Fallback applies to connection failures and HTTP 429/502/503/504 **before streaming starts**. A started stream is never silently replayed. Missing completion markers are recorded as incomplete. Client cancellation closes the upstream connection; stopping provider-side computation is best effort.

The independent Python client is in [sdk/pliqra_client.py](../sdk/pliqra_client.py). Run with `PYTHONPATH=sdk` or copy the module into your application:

```python
from pliqra_client import Pliqra

client = Pliqra("http://localhost:8088", api_key)
connection = client.connect("your-service", ["chat", "streaming"])
result = connection.complete([{"role": "user", "content": "Hello"}])
# Contract changes raise ContractChanged (HTTP 409); explicitly discover/bind again.
```

See [the protocol](PROTOCOL.md) for the handshake, portability boundary, and independent server. Product releases, stable protocol 1.0 and SDK versions have separate lifecycles. The 0.1 envelope remains for existing clients. This is a maintained project contract, without an industry-standard claim.

## Local development and validation

Python 3.13+:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
PLIQRA_DATA_DIR=./data .venv/bin/python -m pliqra
```

Tests use isolated temporary databases and synthetic upstreams. They exercise inference, fragmented/incomplete streams, fallback, contract invalidation, credentials, scopes, quotas, budgets, team reporting, encrypted capture/cache/backup, restore, SCIM lifecycle, signed/tampered/replayed SAML, and actual asynchronous evaluations. `tests/mock_provider.py` is explicitly synthetic and excluded from the production image. It is never launched by the product.

See [release validation](RELEASE.md) for measured evidence and [future scope](ROADMAP.md) for additions beyond this release.

For supported request fields, checked response delivery and the stricter 1.2 compatibility boundary, see [the inference API guide](INFERENCE_API.md).

See the 2.0 [model interfaces](MODEL_INTERFACES.md), [workflows](WORKFLOWS.md), [scale topology](PLATFORM.md) and [language clients/conformance](CONFORMANCE.md) for the expanded surfaces. The existing typed chat boundary remains in force.
