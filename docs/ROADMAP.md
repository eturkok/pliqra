# Roadmap delivery — 2.1

The five engineering tracks previously listed as future scope are implemented in 2.0. Default installation remains endpoint-first, platform-independent and one container. The earlier roadmap was a scope boundary, not a tracked delivery plan; 2.0 ties each track to implementation, UI/API controls, tests and operating guides.

| Track | Delivered | Evidence / guide |
|---|---|---|
| Identity and secrets | Workload JWT federation, secret references, signed SAML logout, SCIM enterprise fields and vendor claim profiles | [Compatibility matrix](IDENTITY_COMPATIBILITY.md), real Keycloak 26.8.2 HTTPS login, signed/replay fixtures |
| Storage and scale | PostgreSQL, replica fencing, shared quotas/concurrency, separate organizations, analytics exports | [Deployment](PLATFORM.md), real two-replica and database tests |
| Quality and protocol | External judges, fresh-evidence publication gates, controlled canaries, extension negotiation | [Workflows](WORKFLOWS.md), failed/stale evidence and shared allocation tests |
| Model workloads | Embeddings, audio, stateless native APIs, Ollama tools, immutable prompts, durable batches | [Interfaces](MODEL_INTERFACES.md), boundary/guardrail/accounting/recovery tests |
| Clients and interoperability | Python, JavaScript/TypeScript, Go; executable core conformance profile; independent reference server | [Conformance](CONFORMANCE.md), real HTTP against both implementations |

See [release validation](RELEASE.md) and [synthetic acceptance](ACCEPTANCE.md) for acceptance details.

## External acceptance remains external

Entra/Okta customer tenants, actual Vault and NeMo deployments, intended real models and provider billing need environment-specific acceptance. Native APIs expose documented subsets; Responses/Messages text and tools now stream incrementally, while full-output checks retain buffering. [Synthetic acceptance](ACCEPTANCE.md) adds packaged clients, independent fleet management and repeatable synthetic acceptance. Third-party protocol attestation, ecosystem adoption and an industry-standard status cannot be delivered by implementing gateway code. The executable suite is available for those participants; none of their approval or adoption is claimed.

Database failover infrastructure, model training/deployment, universal vendor API parity and autonomous production-quality rollout are outside the delivered product boundary. These are explicit limits, not completed checkboxes. See the corresponding guides before choosing a topology.

## Multi-DC follow-through in 2.1

Active/active gateways now have explicit sites/regions, shared strict request/token/concurrency admission, local model preference and placement constraints, per-site health/circuits, audited drain/standby controls, readiness/liveness probes and safe process rejoin after fencing. [Deployment and evidence](MULTI_DC.md) distinguish the tested local two-site environment from actual geographic/WAN and PostgreSQL promotion acceptance. Independent disconnected writers and offline quota grants are not part of this consistency-first mode.
