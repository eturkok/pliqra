<p align="center">
  <img src="docs/assets/pliqra-mark.svg" width="72" height="72" alt="Pliqra">
</p>

<h1 align="center">Pliqra</h1>
<p align="center"><strong>Plug in models. Publish your rules. Keep the contract.</strong><br>Turn a model endpoint into an AI service with an explicit connection contract.</p>
<p align="center">
  <a href="https://github.com/eturkok/pliqra/actions/workflows/test.yml"><img src="https://github.com/eturkok/pliqra/actions/workflows/test.yml/badge.svg" alt="Tests"></a>
  · <a href="https://pliqra.etlabio.com">Website & interactive demo</a>
  · <a href="#quick-start">Quick start</a>
  · <a href="#kubernetes">Kubernetes</a>
  · <a href="#multi-dc">Multi-DC</a>
  · <a href="docs/CONSOLE.md">Console tour</a>
  · <a href="docs/ARCHITECTURE.md">Architecture</a>
  · <a href="docs/PROTOCOL.md">Protocol</a>
</p>

**Bring an endpoint. Publish a gateway. Keep the agreement visible.**

Pliqra combines a model-as-a-service catalog, inference routing, custom gateway policies and guardrails in one self-hosted application. Configure it through the responsive console or API, then connect your applications with scoped keys.

## Why choose Pliqra?

- **Make model changes an application decision.** Your support app relies on tool calling. An administrator changes the registered endpoint, capabilities or gateway policy: bound calls return `409 ContractChanged` until the application explicitly accepts the new contract. [Connection contracts →](docs/PROTOCOL.md)
- **Review one policy across independent installations.** Finance needs the same limits and guardrails in three workspaces. Prepare a plan for each target, get a different administrator's approval, then inspect delivery receipts and policy drift. [Fleet governance →](docs/FLEET.md)
- **Require evidence before a model rollout.** Send 10% of traffic to a candidate only after it meets the configured quality and regression gates. A changed dataset, judge or service invalidates the old evidence. [Controlled releases →](docs/WORKFLOWS.md)

**Pliqra Connection Protocol 1.0 is stable**, with published [normative rules and compatibility policy](docs/protocol/1.0/SPEC.md). Existing 0.1 SDKs and accepted bindings remain compatible.

Bindings are opt-in and track registered service/policy configuration. Evaluation gates cover the selected cases and evaluator. Start with the gateway, console and SQLite in one container; add shared PostgreSQL when you need multiple active sites.

![Evidence-gated model publication in Pliqra](docs/screenshots/workflows-quality.png)

## New in 2.4

**Watch behavior behind an unchanged endpoint.** Schedule bounded response, streaming, JSON and tool checks. See the last successful test, outcome changes and recovery in **Model health**, with daily limits and project budget accounting. A provider can change the model behind the same URL; chosen assertions make observable regressions visible without changing traffic automatically. [Setup, APIs and real-endpoint evidence →](docs/BEHAVIOR_CHECKS.md)

The 2.3 application impact reports and existing-client contract protection remain available. [Review before publication and protect an existing client →](docs/CONTRACT_CHANGES.md)

## What Pliqra brings

| Capability | What you get |
|---|---|
| **Explicit model contracts** | Separate declared capabilities from verified behavior. Bind applications to a fingerprint; changed contracts require explicit renewal with HTTP 409. |
| **A gateway for every workload** | Publish selected services with project/team access, request and token quotas, rolling windows, budgets and concurrency controls. Assigned keys enforce their gateway policy across inference paths. |
| **Composable guardrails** | Chain local checks, NeMo check-API providers and custom HTTP validators on input/output. Enforce or observe; apply local redaction and inspect recorded verdicts. |
| **An explanation for every call** | Follow routing, fallback attempts, guardrail decisions, timing, tokens and estimated costs. Administrators see cumulative totals and team-level detail. |
| **Publish with evidence** | Version prompts, queue durable batches, compare models with external judges, and gate canary releases on fresh quality evidence. |
| **Observe behavior over time** | Regularly test selected expectations at the same endpoint; retain last success, surface changes and notify on failure/recovery without changing traffic automatically. |
| **One policy across interfaces** | Chat, embeddings, audio and native APIs through one admission/accounting boundary, with live Responses/Messages text and tool streaming. |
| **Govern independent installations** | Pair workspaces, filter combined usage, review policy changes with a second administrator, and track delivery and drift from one console. |
| **Start small, scale across sites** | One Docker image by default. Add PostgreSQL, isolated organizations, analytics and active/active data centers when needed. |
| **Identity without platform lock-in** | OIDC/SAML/SCIM, signed logout, short-lived workload identities and environment/file/Vault secret references. |

## Quick start

**Want to try your own endpoints locally?** Start Docker, then run `./scripts/local-demo.sh start` from this checkout. The real product uses a separate persistent demo workspace. [Local demo, endpoint addresses and stop/start →](docs/LOCAL_DEMO.md)

With Docker Compose and a running model endpoint, use the **prebuilt AMD64/ARM64 image**:

```sh
mkdir pliqra && cd pliqra
curl -fL -o compose.release.yaml https://github.com/eturkok/pliqra/releases/download/v2.4.1/compose.release.yaml
docker compose -f compose.release.yaml pull
docker compose -f compose.release.yaml up -d
```

Open **http://localhost:8088**, create your administrator, then follow **connect endpoint → make a real call → connect application**. Your workspace starts empty; the model endpoint must be reachable from inside the container.

**2.4.1 is the stable community release**, adding Light / Dark / System appearance alongside durable model behavior monitoring, application impact reports and contract protection. No source checkout, local build or registry login is required. The 2.4.0 → 2.4.1 update keeps schema 7 and existing contracts; upgrades from 2.3 migrate schema 6 → 7. Back up first and upgrade all writers together. [Release notes & downloads →](https://github.com/eturkok/pliqra/releases/tag/v2.4.1)

[Python SDK](https://pypi.org/project/pliqra-client/) · [JavaScript / TypeScript SDK](https://www.npmjs.com/package/@eturkok/pliqra) · [Install clients →](docs/SDK.md)

[Image verification & upgrades →](docs/DISTRIBUTION.md) · [Source build & first call →](docs/GETTING_STARTED.md) · [Two-replica installation →](docs/PLATFORM.md#start-two-gateways)

## Kubernetes

Use the **official Helm chart** with the prebuilt image. Start with one SQLite pod and retained storage; use external PostgreSQL and existing Secrets for multiple replicas.

```sh
curl -fL -o image.txt https://github.com/eturkok/pliqra/releases/download/v2.4.1/image.txt
helm upgrade --install pliqra https://github.com/eturkok/pliqra/releases/download/helm-v0.3.1/pliqra-0.3.1.tgz --namespace pliqra --create-namespace --wait \
  --set-string image.tag=2.4.1 \
  --set-string image.digest="$(cut -d @ -f 2 image.txt)"
kubectl -n pliqra port-forward service/pliqra 8088:8080
```

The command explicitly selects Gateway 2.4.1. Chart 0.3.1 defaults to 2.2.1; see the [distribution guide](docs/DISTRIBUTION.md#kubernetes-chart-distribution) for image version selection. Create the administrator privately before enabling HTTPS ingress. The chart includes health probes, resource controls, PostgreSQL-only autoscaling, failure-domain placement, optional authenticated monitoring/alerts and a Helm test. Upgrades use Recreate; plan an interruption and back up the database and key first.

[Helm values, PostgreSQL, multi-DC & upgrades →](docs/KUBERNETES.md) · [Operations & recovery drills →](docs/OPERATIONS.md) · [Chart package & cluster test reports →](https://github.com/eturkok/pliqra/releases/tag/helm-v0.3.1)

## Multi-DC

Run active gateways across data centers with **one shared writable PostgreSQL authority**. Prefer local models, enforce global request/token quotas and concurrency limits, and control site draining or standby through the console or API.

Admitted calls can finish during draining. Disconnected sites stop admitting new calls; started calls are not replayed after recovery. Your infrastructure provides PostgreSQL HA and global ingress.

[Setup, architecture & failure behavior →](docs/MULTI_DC.md) · [Multi-DC deployment file →](compose.multidc.yaml)

## Go deeper

| Guide | Contents |
|---|---|
| [Kubernetes & Helm](docs/KUBERNETES.md) | Official chart, SQLite/PostgreSQL profiles, Secrets, ingress, upgrade and cluster acceptance |
| [Releases & prebuilt images](docs/DISTRIBUTION.md) | Versioned Docker images, checksums, SBOM/provenance, publication pipeline and upgrades |
| [Gateways & guardrails](docs/GATEWAYS.md) | Policy builder, quota scopes, check providers, guarded streaming, APIs and request flow |
| [Console tour](docs/CONSOLE.md) | Model onboarding, contracts, request inspection, team reporting and gateway screenshots |
| [Identity & access](docs/ACCESS.md) | Administrator visibility, project/team grants, OIDC, SAML and SCIM |
| [Operations & recovery](docs/OPERATIONS.md) | Configuration, HTTPS, storage, backups, upgrades and operating limits |
| [Architecture](docs/ARCHITECTURE.md) | Components, lifecycle diagrams, authorization, accounting and source map |
| [Connection protocol](docs/PROTOCOL.md) | Discover, bind and invoke; independent SDK and reference server |
| [Capability reference](docs/CAPABILITIES.md) | Full feature matrix and management API entry points |
| [Prompts, batches & releases](docs/WORKFLOWS.md) | Immutable prompts, durable execution, external judges and canary gates |
| [Model interfaces](docs/MODEL_INTERFACES.md) | Embeddings, audio and native API compatibility boundaries |
| [Active/active data centers](docs/MULTI_DC.md) | Shared quota authority, local routing, DC drain and safe recovery |
| [Scale & organizations](docs/PLATFORM.md) | PostgreSQL, distributed admission, isolated workspaces and analytics |
| [Identity compatibility](docs/IDENTITY_COMPATIBILITY.md) | Real Keycloak evidence, vendor profiles, workload trust and secret sources |
| [Fleet management](docs/FLEET.md) | Independent workspaces, combined visibility, reviewed policy rollout and drift |
| [Install application SDKs](docs/SDK.md) | Python / JavaScript packages and checked release artifacts |
| [Synthetic customer acceptance](docs/ACCEPTANCE.md) | Installed applications, load/soak measurements, interruption and recovery evidence |
| [Clients & conformance](docs/CONFORMANCE.md) | Python, JavaScript/TypeScript, Go and an executable protocol profile |
| [Performance comparison](docs/PERFORMANCE.md) | Direct model/TLS controls, matched before/after measurements and remaining limits |
| [Technical architecture review](docs/TECHNICAL_REVIEW.md) | Findings, corrected designs, regression evidence and deployment boundaries |
| [Validation & scope](docs/RELEASE.md) | Automated/runtime evidence, exercised flows and operating limits |

[Stable readiness & support →](docs/STABLE_2_1.md) · [Compatibility & limits →](docs/SUPPORT.md)

**Stable release: [2.4.1](https://github.com/eturkok/pliqra/releases/tag/v2.4.1).** The default remains a single container. Protocol 1.0 with legacy 0.1 compatibility; native APIs are documented subsets. [Roadmap delivery](docs/ROADMAP.md) separates implemented scope from external vendor acceptance and ecosystem adoption.

## License

[Apache-2.0](LICENSE) — free for personal and commercial use, modification and redistribution under its terms. Paid support and separately licensed future modules can be offered independently. [Licensing details →](docs/LICENSING.md)
