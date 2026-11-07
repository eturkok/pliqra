<p align="center">
  <img src="docs/assets/pliqra-mark.svg" width="72" height="72" alt="Pliqra">
</p>

<h1 align="center">Pliqra</h1>
<p align="center"><strong>Plug in models. Publish your rules. Keep the contract.</strong><br>Turn a model endpoint into an AI service with an explicit connection contract.</p>
<p align="center">
  <a href="https://github.com/eturkok/pliqra/actions/workflows/test.yml"><img src="https://github.com/eturkok/pliqra/actions/workflows/test.yml/badge.svg" alt="Tests"></a>
  · <a href="#quick-start">Quick start</a>
  · <a href="#multi-dc">Multi-DC</a>
  · <a href="docs/CONSOLE.md">Console tour</a>
  · <a href="docs/ARCHITECTURE.md">Architecture</a>
  · <a href="docs/PROTOCOL.md">Protocol</a>
</p>

**Bring an endpoint. Publish a gateway. Keep the agreement visible.**

Pliqra combines a model-as-a-service catalog, inference routing, custom gateway policies and guardrails in one self-hosted application. Configure it through the responsive console or API, then connect your applications with scoped keys.

![Evidence-gated model publication in Pliqra](docs/screenshots/workflows-quality.png)

## What Pliqra brings

| Capability | What you get |
|---|---|
| **Explicit model contracts** | Separate declared capabilities from verified behavior. Bind applications to a fingerprint; changed contracts require explicit renewal with HTTP 409. |
| **A gateway for every workload** | Publish selected services with project/team access, request and token quotas, rolling windows, budgets and concurrency controls. Assigned keys enforce their gateway policy across inference paths. |
| **Composable guardrails** | Chain local checks, NeMo check-API providers and custom HTTP validators on input/output. Enforce or observe; apply local redaction and inspect recorded verdicts. |
| **An explanation for every call** | Follow routing, fallback attempts, guardrail decisions, timing, tokens and estimated costs. Administrators see cumulative totals and team-level detail. |
| **Publish with evidence** | Version prompts, queue durable batches, compare models with external judges, and gate canary releases on fresh quality evidence. |
| **One policy across interfaces** | Chat, embeddings, audio and native APIs through one admission/accounting boundary, with live Responses/Messages text and tool streaming. |
| **Govern independent installations** | Pair workspaces, filter combined usage, review policy changes with a second administrator, and track delivery and drift from one console. |
| **Start small, scale across sites** | One Docker image by default. Add PostgreSQL, isolated organizations, analytics and active/active data centers when needed. |
| **Identity without platform lock-in** | OIDC/SAML/SCIM, signed logout, short-lived workload identities and environment/file/Vault secret references. |

## Quick start

With Docker Compose and a running model endpoint, use the **prebuilt AMD64/ARM64 image**:

```sh
mkdir pliqra && cd pliqra
curl -fL -o compose.release.yaml https://github.com/eturkok/pliqra/releases/download/v2.1.0-rc.4/compose.release.yaml
docker compose -f compose.release.yaml pull
docker compose -f compose.release.yaml up -d
```

Open **http://localhost:8088**, create your administrator, then follow **connect endpoint → make a real call → connect application**. Your workspace starts empty; the model endpoint must be reachable from inside the container.

**2.1.0-rc.4 is a release candidate for evaluation.** No source checkout, local build or registry login is required. [Release notes & downloads →](https://github.com/eturkok/pliqra/releases/tag/v2.1.0-rc.4)

[Python SDK](https://pypi.org/project/pliqra-client/) · [JavaScript / TypeScript SDK](https://www.npmjs.com/package/@eturkok/pliqra) · [Install clients →](docs/SDK.md)

[Image verification & upgrades →](docs/DISTRIBUTION.md) · [Source build & first call →](docs/GETTING_STARTED.md) · [Two-replica installation →](docs/PLATFORM.md#start-two-gateways)

## Multi-DC

Run active gateways across data centers with **one shared writable PostgreSQL authority**. Prefer local models, enforce global request/token quotas and concurrency limits, and control site draining or standby through the console or API.

Admitted calls can finish during draining. Disconnected sites stop admitting new calls; started calls are not replayed after recovery. Your infrastructure provides PostgreSQL HA and global ingress.

[Setup, architecture & failure behavior →](docs/MULTI_DC.md) · [Multi-DC deployment file →](compose.multidc.yaml)

## Go deeper

| Guide | Contents |
|---|---|
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
| [Technical architecture review](docs/TECHNICAL_REVIEW.md) | Findings, corrected designs, regression evidence and deployment boundaries |
| [Validation & scope](docs/RELEASE.md) | Automated/runtime evidence, exercised flows and operating limits |

**Current candidate: [2.1.0-rc.4](https://github.com/eturkok/pliqra/releases/tag/v2.1.0-rc.4).** The default remains a single container. Protocol draft 0.1; native APIs are documented subsets. [Roadmap delivery](docs/ROADMAP.md) separates implemented scope from external vendor acceptance and ecosystem adoption.

## License

[Apache-2.0](LICENSE) — free for personal and commercial use, modification and redistribution under its terms. Paid support and separately licensed future modules can be offered independently. [Licensing details →](docs/LICENSING.md)
