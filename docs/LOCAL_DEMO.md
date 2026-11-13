# Local demo with your own models

Run the real **Pliqra 2.5.0** product locally, backed by a separate persistent workspace. This is not the website simulation or the synthetic acceptance lab. Bring a running model endpoint; Pliqra does not download or run model weights.

## Start and stop

Start Docker Desktop, then run from the repository root:

```sh
./scripts/local-demo.sh start
```

Open **http://localhost:8088**. Create your administrator account on first use. No default password, API key, model, paid test or example traffic is seeded.

```sh
./scripts/local-demo.sh status
./scripts/local-demo.sh stop
# Later, start again to continue with the same workspace:
./scripts/local-demo.sh start
```

The demo uses the published AMD64/ARM64 image pinned by digest, one CPU and up to 512 MiB RAM. The image is downloaded only if missing; no local build is required. The container does not restart automatically when Docker starts. Stopping it preserves the `pliqra-demo-data` volume, including the database and encryption key. The standard `pliqra-data` workspace is separate. Keep the demo volume; `down --volumes` or manual volume removal deletes its data.

Port 8088 is bound only to this computer. If it is already occupied, use `PLIQRA_DEMO_PORT=9090 ./scripts/local-demo.sh start` and open http://localhost:9090. Use the same port setting when starting again. Status, stop and logs address the same Compose project regardless of the current directory.

## Bring an endpoint

In **Overview → Connect endpoint**, choose the provider, endpoint and any required API key. Pliqra connects from inside Docker, so a model running on this computer uses **`host.docker.internal`** in place of `localhost`:

| Your model server | Endpoint entered in Pliqra | Provider |
|---|---|---|
| OpenAI-compatible server on port 8000 | `http://host.docker.internal:8000/v1` | OpenAI-compatible |
| OpenAI-compatible server on port 1234 | `http://host.docker.internal:1234/v1` | OpenAI-compatible |
| Ollama on port 11434 | `http://host.docker.internal:11434` | Ollama |
| Remote model service | Its reachable HTTPS endpoint | Matching provider |

Use your server's actual port and address. A remote service keeps its real hostname. Enter credentials in the console; stored provider secrets are encrypted in the demo volume. Servers without model discovery can use an explicit model name. For private certificate authorities, see [CA configuration](OPERATIONS.md#https-and-storage); retain certificate validation.

## Try the product

1. **Connect and call:** follow Overview, then send a prompt in Playground. Request details show the actual route, timing and reported usage.
2. **Create a gateway:** choose services and project/team access, add request/token limits, and create a scoped application key.
3. **Connect an application:** accept a binding and use the generated client settings. [Existing OpenAI client protection](CONTRACT_CHANGES.md) is optional.
4. **Observe behavior:** in Model health, create a schedule and run a first baseline. Choose a daily limit and enable periodic checks when wanted. These are real model calls. [Configuration and limits](BEHAVIOR_CHECKS.md).
5. **Review usage and recovery:** filter Usage & cost, inspect requests, and create an encrypted backup in Operations. Configure the deployment's prices if you want monetary budget accounting; missing prices stay unknown.

The demo initially has one SQLite workspace. Shared PostgreSQL, real identity providers and external guardrail engines need their own configuration; the default demo does not start those services. [Full setup](GETTING_STARTED.md) · [Operations](OPERATIONS.md).
