# Support and compatibility — 2.2

[← Pliqra](../README.md)

**2.3.0 is the stable community release of the documented self-hosted gateway.** Stable means the release passes its committed acceptance gates and maintains the compatibility policy below. It is not a capacity, availability, provider-safety or identity-vendor certification. Community issues are best-effort; no response-time, uptime, RTO or RPO commitment is bundled with Apache-2.0.

## Supported deployment envelope

| Area | Supported scope and evidence |
|---|---|
| Distribution | One non-root Linux container, AMD64 or ARM64; native architecture startup checks. Docker Desktop uses the matching Linux image. |
| Kubernetes | Official chart 0.3.1; Helm 4.3.1, Kubernetes 1.34.11/1.37.0 and both storage profiles tested in disposable single-node kind clusters. No vendor, storage-driver or multi-zone availability certification. [Installation and test scope](KUBERNETES.md). |
| Default storage | One SQLite/WAL process per persistent volume. Preserve the entire volume and its encryption key. |
| Shared storage | PostgreSQL 18 is the tested baseline. Replicas share a schema and master key; one writable database authority owns policies and exact admission. |
| Active/active sites | Local-preference/placement routing, global admission, site drain and fenced-process recovery. The container exercise runs on one host; geographic WAN and database primary promotion remain operator acceptance. |
| Python runtime | 3.13 and 3.14 in CI; the image uses Python 3.13 and locked dependencies. |
| Packaged SDKs | Python 3.10+ and Node 22+ ES modules. Clean artifact installation is checked on Python 3.10/3.14 and Node 22/24; installed application calls are exercised on the acceptance runner. |
| Console | Responsive browser console checked at 320–1440 px, including keyboard navigation and bounded dialogs. Real-device and cross-engine certification are not claimed. |
| Inference | The [documented Chat/native/embedding/audio subsets](MODEL_INTERFACES.md), admission, guardrails and accounting. Complete-output guardrails deliberately buffer output. |
| Identity / guardrails | Documented OIDC/SAML/SCIM behavior, real Keycloak evidence and signed fixtures. Actual Entra/Okta/NeMo/Vault deployments need their environment-specific acceptance. |
| Operations | Fresh install, published-image SQLite 2.1-to-2.2 upgrade with persistent credentials/policies/quotas, encrypted backup import, session revocation and recovery of the pre-upgrade backup on 2.1.0 in separate storage. |
| Chart operations | Isolated SQLite/PostgreSQL archive recovery, configuration-revision rollout and monitoring-token authorization. Optional Prometheus Operator 0.95.1 CRD schemas and promtool 3.16.0 rule tests; actual Operator discovery, Alertmanager delivery and backup RPO/RTO need deployment acceptance. |

The predeclared synthetic release envelope is **eight clients, 30 seconds of bounded load and 30 minutes at a target five calls/second**, with zero unsuccessful load/soak calls, at least 8,000 soak calls, p95 ≤2,000 ms and sampled west-gateway RSS growth ≤64 MiB. These are regression thresholds for a deterministic 5 ms fixture on a CI runner. They are **not** model token-throughput guarantees, customer sizing advice or days of endurance evidence. Full results identify the actual source/image, runner, duration and observations.

## Compatibility and upgrades

- Gateway releases use `MAJOR.MINOR.PATCH`. A 2.2.x patch preserves documented management/inference contracts and storage compatibility; intentional breaks require a new minor/major release with an upgrade guide. Undocumented implementation details are not a compatibility promise.
- Gateway **2.3.0**, SDK **1.0.1**, protocol **pliqra/1.0** (specification **1.0.0**), database schema **6** and inference contract **5** are independently versioned. [The protocol lifecycle](protocol/1.0/SPEC.md#8-compatibility-and-maintenance-policy) preserves the 0.1 compatibility envelope throughout gateway 2.x. Stable is a project compatibility commitment, without an industry-standard or independent-certification claim.
- Service fingerprints still change when their declared model, gateway, placement or capability contract changes. HTTP 409 and explicit binding renewal are expected product behavior, not a patch compatibility failure. 2.1-to-2.2 uses the same schema/inference revision and tests an unchanged binding across the upgrade.
- Stop all shared-state replicas, back up and upgrade together. Preserve one writable authority; no offline quota grants are supported. Do not use independent writable DC databases as a substitute for database HA.
- Returning to an older image uses its **pre-upgrade backup** in separately prepared storage. Never assume an older image can downgrade a newer database. The release acceptance exercises that recovery path.

## Limits and responsibilities

Configured request/token limits, retention and concurrency ceilings are policy values, not certified capacity. Missing provider usage stays unknown and conservatively reserved; configured-price costs are estimates, not invoices. The in-product archive has a 32 MB database/plaintext limit; larger deployments use native database backups together with the master key. The [operations guide](OPERATIONS.md) and [API subset guide](MODEL_INTERFACES.md) define the remaining concrete limits.

Global ingress, verified TLS, PostgreSQL replication/promotion/durability, host patches, model capacity, regional network behavior, actual IdP/guardrail integrations and workload-specific acceptance belong to the installation. Run a staged pilot with your endpoint before broad rollout. Unavailable infrastructure, unaudited third-party safety, commercial support or external adoption are not asserted by the stable version number.

[Protocol 1.0 and release evidence](evidence/protocol-1.0.json) · [Previous 2.1 baseline](STABLE_2_1.md) · [Security policy](../SECURITY.md) · [Issue tracker](https://github.com/eturkok/pliqra/issues)
