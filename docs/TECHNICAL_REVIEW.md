# Pliqra — technical architecture review

> Current release: Gateway 2.2.1 implements stable [Connection Protocol 1.0](protocol/1.0/SPEC.md), retaining the 0.1 envelope for legacy clients. Draft references in the dated review/migration history below describe earlier releases.

## End-to-end review — 11 November 2026

**Baseline:** `a2e9db1` (published Gateway 2.2.0 / Protocol 1.0). **Status:** corrections merged through [PR #1](https://github.com/eturkok/pliqra/pull/1) for Gateway **2.2.1** and SDK **1.0.1**. Publication requires the normal exact-artifact release gates; published 2.2.0/1.0.0 artifacts remain immutable.

### Architectural assessment

The modular monolith, one writable PostgreSQL quota authority and independently versioned client/protocol contracts fit the endpoint-first product. The necessary corrections belong in existing authentication, admission, transport and rendering boundaries. Shared helpers now govern provider-attempt creation, checked-input reservations, bearer parsing and server SSE framing. Storage ownership, the protocol wire identifier, schema 6 and inference contract 5 are preserved.

The review followed authentication → service access → binding → admission → guardrails → Secret resolution → provider IO → settlement/reporting, then checked recovery, organization isolation, SDKs, console rendering and distribution controls. Findings were reproduced with synthetic credentials/providers and isolated storage; the tests assert observable behavior rather than implementation structure. This is a scoped engineering review, not a full ASVS assessment or external certification.

### Findings and corrections

| ID | Priority | Reproduced issue and effect | Implemented correction / regression evidence |
|---|---|---|---|
| AR-01 | P1 | A password could change after verification but before session insertion. The old password then produced a new valid session after the reset had revoked existing sessions. | Session issuance revalidates active identity/provider state and the verified local-password hash inside its transaction. `test_password_change_during_login_verification_cannot_create_a_session` rejects that interleaving without inserting a session. |
| AR-02 | P1 | A process fenced while awaiting a guardrail or external Secret could start new model work afterwards, then return 503 during settlement. | `Gateway.start_attempt` rechecks the replica lease after asynchronous preparation, immediately before provider IO, and records the attempt in the same transaction. Chat and embedding/audio paths share it. Four fault-injection cases assert 503, zero provider calls and zero attempts. A local release flag is set before fallible storage cleanup. |
| AR-03 | P1 | Redaction could expand input after quota/budget admission, leaving reservations based on the smaller original input. | Changed checked input is validated again against input bounds. Additional project budget and token capacity are reserved in one workspace transaction, using the original admission window and budget month. Failure starts no provider call, frees unused reservations and retains one request record. Serial/concurrent chat and embedding cases cover 402/422/429; PostgreSQL tests exercise two independent site replicas. |
| AR-04 | P2 | Sequential keyword replacements could match their own `[REDACTED]` markers, corrupting output and amplifying content repeatedly. | One combined escaped pattern scans each original value once. A four-megabyte encoded-content growth budget stops oversized redaction during replacement, before constructing an unbounded result. Tests cover overlapping/repeated terms and fail-closed growth exhaustion. |
| AR-05 | P2 | Chat/server and SDK SSE readers treated lines as events. Valid multiline data, BOM or CR framing could fail; a terminal marker lacking its final blank line could be accepted as success. Native parsing also dispatched pending data at EOF. | Server and all three SDKs frame complete SSE events, handle LF/CRLF/CR and BOM, concatenate data fields, bound lines/events/streams and discard unfinished events. Heartbeat forwarding, incremental delivery, cancellation and no automatic inference replay are retained. Tests cover standard framing and incomplete terminal events. |
| AR-06 | P2 | Imported deployment-reference names reached quoted form attributes without encoding and could introduce HTML elements. CSP reduced script execution exposure but did not make the rendering safe. | Existing `field`/`selectField` helpers encode names in `for`, `id` and `name`. Node tests render the actual console helpers with hostile references; no element/event-handler injection is produced and ordinary mapping names are preserved. |
| AR-07 | P2 | Authentication-scheme matching was case-sensitive, so a valid lowercase bearer header could fall back to a browser's administrator cookie. 401 challenges were missing; SCIM/metrics lacked the project's no-store policy. JSON media-type matching was case-sensitive too. | One bearer parser handles scheme casing for inference, SCIM, metrics and fleet. An explicit unsupported Authorization header is rejected rather than falling back to a cookie. 401 responses include a challenge; protected metadata gets no-store. Valid mixed-case JSON types are accepted. Tests prove key quota/attribution with an administrator cookie present. |
| AR-08 | P3 | Python SDK HTTP-error bodies were read without deterministically closing the response. Repeated rejected calls could retain transport resources until garbage collection. | Error parsing uses a response context; a retained fake HTTPError verifies the underlying response is closed while the typed status is preserved. |

### Standards and engineering decisions

| Reference | Applied scope and boundary |
|---|---|
| [RFC 9110 §§8.3.1, 11.1, 11.6.1](https://www.rfc-editor.org/rfc/rfc9110.html) | Case-insensitive media/auth scheme tokens and a WWW-Authenticate challenge on 401. Existing status/body contracts remain available. No-store is the project's explicit private-response policy. |
| [WHATWG SSE §§9.2.5–9.2.6](https://html.spec.whatwg.org/multipage/server-sent-events.html#parsing-an-event-stream) | UTF-8/BOM, all three line endings, multiline data and blank-line dispatch. Inference clients consume framing; they intentionally do not implement EventSource automatic reconnection/replay. |
| [OWASP authentication guidance](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html) | Recheck credential/lifecycle state when issuing a session; preserve CSRF, cookie and federation boundaries. The review does not claim every authentication recommendation has been certified. |
| [OWASP API4 resource consumption](https://owasp.org/API-Security/editions/2023/en/0xa4-unrestricted-resource-consumption/) and [attribute encoding](https://cheatsheetseries.owasp.org/cheatsheets/Cross_Site_Scripting_Prevention_Cheat_Sheet.html#output-encoding-for-html-attribute-contexts) | Bound transformations and actual checked-input reservations; encode untrusted form-attribute data. CSP remains an additional control. |
| [Kubernetes probe semantics](https://kubernetes.io/docs/concepts/workloads/pods/probes/) | Existing independent liveness/readiness/startup checks, non-root/read-only containers, retained SQLite volume, PostgreSQL Secret references, fenced-process restart and digest-pinned chart were reviewed. Existing chart lifecycle is retained. |

### Validation and release boundary

Local Python 3.14 validation: **285 passed**, **35 PostgreSQL-dependent cases explicitly skipped** without a database URL. All three language runtimes were required for the HTTP compatibility matrix. JavaScript SDK assertions, Go tests/`go vet`, three console encoding tests and Helm lint/35 render checks passed. [CI for the review branch](https://github.com/eturkok/pliqra/actions/workflows/test.yml?query=branch%3Acodex%2Farchitecture-review) additionally runs Python 3.13/3.14, real PostgreSQL, packaged clients, Docker startup and the dependency audit.

- `tests/test_review_boundaries.py` contains focused behavioral reproductions. Existing protocol HTTP compatibility, native first-content/cancellation, identity, recovery and accounting tests run alongside them.
- JavaScript SDK tests include seven new framing/EOF cases; Go tests and `go vet` cover the same transport boundary. Three console encoding tests run in the clients CI job.
- The real-PostgreSQL CI job additionally checks expanded reservations across two independent gateway sites. Local runs explicitly skip PostgreSQL cases if its URL is not configured.
- Helm lint and 35 render/safety checks passed against the unchanged chart. Chart 0.3.0's earlier cluster evidence remains evidence for its published image, not a claim that this unreleased source patch has been installed in Kubernetes.
- A future container/SDK release must run its normal exact-artifact publication gates. The existing 2.2.0/1.0.0 release evidence is historical and is not relabelled as evidence for these corrections.

### Operational considerations

Shared PostgreSQL writes intentionally serialize quota/policy decisions. Its synchronous connection and WAN latency remain capacity constraints; profile a representative workload before introducing pools or changing that authority. Local-password sign-in throttling is currently an in-process IP limit, not a global multi-replica abuse-control service; deployments exposing local login need ingress/IdP abuse controls and should test their proxy/client-address setup. Organization archives remain separate recovery units. Actual IdP/NeMo integrations, geographic failure and recovery objectives require the installation's acceptance tests.

## Historical 1.2 review — 6 November 2026

- **Date:** 6 November 2026
- **Reviewed baseline:** 2f5abc6 — Pliqra 1.1
- **Remediation release:** 1.2

[← Pliqra](../README.md) · [Architecture](ARCHITECTURE.md) · [1.2 validation record](https://github.com/eturkok/pliqra/blob/62e4ec0d02a9a5d85748bf0072ab6d532cc85abd/docs/RELEASE_1_2.md) · [Current validation record](RELEASE.md)

This document records the original 1.2 review and its remediation evidence. The [2.0 follow-through](#20-architecture-follow-through--6-november-2026) below describes the subsequent implementation; current deployment scope and validation are documented separately.

## Executive assessment

A modular application delivered as one Docker image is the architecture most closely aligned with Pliqra's goals. Model servers, identity providers and guardrail engines should remain external resources; Pliqra should govern access, contracts, routing and usage across them. At the reviewed stage, splitting the application into microservices would complicate deployment without addressing a demonstrated production requirement.

The product's distinction extends beyond routing and quotas: the contract accepted by an application, the policy executed and the evidence from the actual call are visible in one workflow. Keeping declared capabilities, verified behavior, reported usage and estimated costs separate is a sound foundation.

The baseline contained gaps between that intention and its implementation. In particular, unrestricted API fields, response processing that checked only the message, authorization checks performed before admission and independently updated accounting records weakened the trust boundary. The findings below were addressed in code and regression tests.

**Assessment:** Release 1.2 provides a more consistent foundation for its documented single-workspace, single-process deployment target. This review does not constitute independent security certification, universal provider compatibility or proof of high availability.

## Scope and method

- Reviewed management, identity and authorization, standard/named/protocol inference paths, guardrail chains, quota and budget records, caching, health checks, encryption and restore workflows.
- Reproduced risky behavior using isolated databases and controlled upstream responses; validated corrections alongside the existing tests.
- Tested races by synchronizing two calls at the same preparation point, record consistency through fault injection, and schema execution through deadlines and event-loop responsiveness.
- Used no real company data, user passwords or live model/NeMo services. This work does not replace acceptance testing with the intended IdP/NeMo deployment or comprehensive load and penetration testing.

P1 identifies policy, data or availability issues that must be addressed first for this deployment target. P2 identifies operational and record-accuracy issues. These priorities are not CVSS scores.

## Findings and implemented corrections

| ID | Priority | Baseline issue | Correction implemented in 1.2 | Evidence |
|---|---|---|---|---|
| TA-01 | P1 | Output redaction changed the message while unchecked text could still escape through logprobs, provider debug fields, model/ID fields or usage metadata. Local rules could also mistake protocol role/type values for content. | Calls with output checks use a controlled response envelope, locally generated metadata and validated message fields. Objects/lists are rejected in text fields. Text transformations preserve protocol roles and type labels. | JSON/SSE side-channel and role/keyword regression tests |
| TA-02 | P1 | Unrestricted extra request fields, ambiguous token controls and legacy `functions`/`function_call` formats could bypass policy semantics. | A separate typed inference schema rejects unknown fields and accepts only one output-limit field. Modern and legacy tool formats share capability and permission checks. Unsupported fields return 422 before any upstream call. | Unknown-control, duplicate-limit, malformed stream-options and legacy-tool tests |
| TA-03 | P1 | Changes to users, sessions, services or verified capabilities between HTTP validation/preparation and quota admission could start calls using stale information. | A consistent preparation snapshot and atomic admission transaction revalidate the current user/key/session, project/service access, service fingerprint, gateway policy and eligible deployments at admission. | User/session revocation, service-change and lost-verification tests |
| TA-04 | P1 | JSON Schema regex/reference processing ran on the main asynchronous event loop; expensive validation could block other requests. | Guardrails and evaluations share at most two temporary schema workers. Deadlines include queue time; cancellation terminates and reaps workers. Linux workers have address-space and CPU limits. External reference resolution is disabled. | Heartbeat responsiveness during expensive regex validation, timeout/child-process cleanup and positive schema tests |
| TA-05 | P1 | SSE/NDJSON line sizes were unbounded. Discovery responses were measured only after being fully loaded into memory. Incomplete completion shapes could be marked successful and verified. | Reads enforce size limits, strict JSON parsing and minimum completion validation. Malformed streams produce controlled incomplete/error outcomes; small frames are delivered without being accumulated. | Oversized stream-frame, first-frame delivery, malformed completion and existing Unicode/incomplete-stream tests |
| TA-06 | P1 | Even after a fallback attempt reported 30,000 known tokens, an unknown-usage subsequent attempt could leave the gateway counter at a reservation of 4,350. Event and ledger results were written separately. | Partial usage preserves at least the known total and the existing reservation. Event and gateway settlement commit in the same transaction. | Partial-fallback and settlement fault-injection tests |
| TA-07 | P1 | The new guardrail secret field was excluded from startup key validation. A backup with a validly formatted key that did not match its encrypted data could be restored. | One registry covers encrypted sources, with streamed validation including guardrails. Restore checks key/ciphertext and foreign-key consistency before changing existing data. | Missing/wrong-key tests for guardrail-only secrets and rejection of invalid archives before mutation |
| TA-08 | P2 | Opening a newer database could cause migration code to write an older schema version back to it. | Schemas newer than the supported version are rejected before migration begins. | Schema 999 rejection with its version left unchanged |
| TA-09 | P2 | Parallel round-robin calls could prepare from the same stale counter and select the same model. | Selection order and persistent cursor advancement occur inside the admission transaction. Rejected calls do not consume the cursor. | Two calls prepared concurrently are distributed across two deployments |
| TA-10 | P2 | A cached fallback response could identify the first candidate as its producing model in headers/events. | Encrypted cache records retain the producing deployment's identity. Cache-hit events/headers use that source, while upstream tokens/cost remain zero. Old cache formats are treated as misses. | Cache provenance after fallback and zero-upstream-usage tests |
| TA-11 | P2 | An outstanding health check could write stale results over a changed endpoint or restored workspace. An active provider test was excluded from the restore barrier. | Health-check success/error writes verify generation/configuration inside a transaction. Provider tests participate in maintenance/concurrency controls and the restore barrier. | Success/error cases across endpoint/workspace changes, and restore rejection during an active check |
| TA-12 | P2 | Named `/g/` paths were excluded from the standard API's no-store boundary. | The same cache-control boundary applies to every inference route. | No-store checks for named model lists and completions |

Sources: [inference boundary](../pliqra/inference_schema.py), [admission and execution](../pliqra/gateway.py), [policies](../pliqra/policies.py), [schema executor](../pliqra/schema_validation.py), [worker](../pliqra/schema_worker.py), [encryption](../pliqra/security.py), [recovery and health](../pliqra/operations.py), [regression tests](../tests/test_architecture.py).

## Corrected execution model

~~~mermaid
flowchart LR
    Request[Typed request] --> Prepare[Consistent service/policy snapshot]
    Prepare --> Admit[Atomic admission]
    Admit --> Auth[Current identity + session + access]
    Auth --> Limits[Quota + budget + concurrency + cursor]
    Limits --> Input[Input checks]
    Input --> Model[Eligible model / safe fallback]
    Model --> Output[Response shape + controlled envelope when required]
    Output --> Checks[Output checks]
    Checks --> Commit[Atomic event + ledger settlement]
    Commit --> Deliver[JSON / checked SSE]
    Schema[Temporary, bounded schema workers] -.-> Input
    Schema -.-> Checks
~~~

This diagram shows the buffered response path with output checks. During live upstream streaming, frames are forwarded after input checks; records are settled on a terminal marker or error. There is no distributed exactly-once guarantee between network delivery and database settlement.

## Architectural decisions to preserve

1. **Modular monolith and one persistent volume by default.** This matches the portable installation goal. Schema workers own neither the API nor the database; they perform temporary validation only.
2. **Separate control-plane and execution boundaries.** Management APIs, the typed inference contract and provider I/O should remain distinct. Adding a vendor field requires defining its quota/capability implications instead of simply forwarding JSON.
3. **Separate declarations, evidence and pricing.** Passing a format check does not establish general model quality or semantic safety. Unknown usage must remain unknown; estimated cost must not be presented as an invoice.
4. **Explicit failure behavior.** Revoked access returns 401/403; changed service/policy contracts require renewal with 409; temporary eligibility failures return 503; unsupported requests return 422. An upstream stream that has started is never replayed.
5. **Observable, bounded side effects.** Tests, probes and evaluations require explicit initiation. Raw content is not retained by default. Child processes and temporary services must not remain running after work finishes.

Release 1.2 added the inference implementation revision to the fingerprint calculation. Existing bindings and cache entries do not silently inherit changed execution semantics; applications explicitly bind to the new contract. That release used wire protocol draft 0.1; Gateway 2.2 promotes the compatible core to stable 1.0.

## Validation and acceptance for 1.2

- Test suite: **92 tests**, preserving the original 51 and adding 41 architecture regression cases.
- Validation on host Python 3.14 and Linux/Python 3.13 in Docker.
- Real HTTP checks in an empty Docker workspace: 1.2.0 health, initial administrator setup, anonymous-access rejection, schema-worker pass/block outcomes, no-store headers and 422 rejection of unknown request fields.
- Coverage includes successful inference, Unicode streaming, fallback, identity/SCIM/SAML, quotas/budgets, backup/restore, gateways/guardrails and the race/failure scenarios above.
- Real-vendor acceptance, soak testing, independent penetration testing and distributed-environment testing were not performed as part of the 1.2 review.

## Deployment decision and next thresholds at the 1.2 baseline

**For the reviewed target:** controlled deployment with one workspace, one server process and a persistent volume, subject to acceptance testing against the organization's actual endpoints and identity policies.

**Requirements that would trigger additional architectural work:**

| Requirement | Work required |
|---|---|
| Multiple active replicas / HA | Shared transaction model, distributed quotas/concurrency, PostgreSQL and failover acceptance tests |
| Physical tenant isolation | Separate data/key/identity boundaries; existing project authorization is not a substitute |
| Very large reporting volumes | Capacity measurement with real workloads; a separate analytics read model if needed |
| Real NeMo/IdP compatibility matrix | End-to-end testing with the organization's versions, claims, certificates and policy configuration |
| Performance/SLO commitments | Load/soak measurements including new worker and guardrail overhead; the old synthetic benchmark must not be reused |

At the 1.2 baseline, these requirements were not delivered capabilities and should not have been marketed as such. Scope and compatibility boundaries are documented in the [operations guide](OPERATIONS.md), [gateway guide](GATEWAYS.md) and [1.2 validation record](https://github.com/eturkok/pliqra/blob/62e4ec0d02a9a5d85748bf0072ab6d532cc85abd/docs/RELEASE_1_2.md). The following section records subsequent delivery in 2.0.

## 2.0 architecture follow-through — 6 November 2026

The previous future-scope list is now linked to delivered features and acceptance evidence in [ROADMAP.md](ROADMAP.md). Storage is explicit rather than an opaque SQL compatibility shim: portable schema/data operations are shared; backend-specific JSON/date/introspection paths are explicit. A real PostgreSQL test caught and corrected a remaining SQLite JSON operation in the onboarding dashboard. Admission and configuration writes use one schema lock, while upstream calls remain outside transactions. This trades write parallelism for deterministic policy correctness.

Replica leases fence stale processes; recovery retains uncertain charges and never replays started inference. Management writes participate in restore barriers. Logical restore is transactional, bounded, key-validated and isolated to the selected schema. Organization sessions and keys are separate, with root-directory administration distinct from tenant data permissions.

Native/binary routes share authentication/admission/accounting and apply guardrails only to inspectable content; incompatible required stages fail closed. Provider-native streaming is labeled buffered. External score evidence includes dataset/judge/service versions, and publication rejects stale or failed evidence. At that checkpoint the protocol was draft; the executable core profile made no vendor-safety or ecosystem claim. Gateway 2.2 publishes the stable 1.0 contract with the same claim boundaries.

[2.0 validation](RELEASE.md) and [deployment limits](PLATFORM.md) record real local interoperability separately from synthetic provider fixtures and unavailable customer tenants. Database HA infrastructure and production capacity still require operator validation.

## 2.1 multi-DC follow-through — 6 November 2026

The active/active design uses one writable PostgreSQL authority for global quota and budget correctness. Site-local endpoint observations replace shared reachability/circuit state in multi-DC mode; one network partition can no longer poison another site's path. Database-clock leases/windows reduce gateway-clock sensitivity. Typed location constraints participate in fingerprints and apply to stable, fallback, canary and cached model selection. Traffic modes are audited and versioned; draining rejects new admission while admitted calls settle. Separate liveness/readiness probes and opt-in supervised process restart prevent fenced processes from silently rejoining with old provider IO.

Real PostgreSQL tests additionally exposed backend-specific token quota issues: BIGINT sums require integer normalization before error serialization, and nullable quota scopes need portable null-safe equality. These were corrected and exercised for gateway/key/user/project/team/service scopes across sites. Candidate and fingerprint inventory reads are batched to reduce WAN round trips. [Multi-DC validation](MULTI_DC.md#evidence-and-upgrade) records the actual test boundary; database promotion, external leader fencing and geographic latency/SLOs remain operator acceptance responsibilities.

## RC3 architecture follow-through — 7 November 2026

Three remaining product gaps now have executable delivery paths. `native_streaming.py` translates bounded native SSE incrementally through the existing admission/accounting boundary; explicit terminal checks prevent a truncated stream from becoming verified success. Complete-output checks retain buffering, and started work is never replayed. Stream cancellation closes nested iterators before releasing capacity.

Standalone SDK artifacts are built from the canonical sources, installed outside the gateway checkout and attached to the release with source revision and hashes. Registry publishing consumes those tested bytes instead of rebuilding them with credentials present. The separate reference implementation and core profile remain independent of gateway imports; synthetic acceptance does not supply third-party adoption or human usability evidence.

Fleet connects independent authorities using explicit scoped delegation. Resource aliases are resolved locally, plans capture current and desired fingerprints, a different active controller administrator approves, and member writes conditionally commit the policy/version/audit/receipt together. A lost commit response is reconciled by operation ID. Partial rollout is explicit, rollback is another reviewed proposal, and stale snapshots are excluded from aggregate totals. Pairing tokens remain privileged within the delegated namespace: member APIs trust the controller rather than cryptographically proving two human approvals. Independent-member quotas are not advertised as one global quota.

The local regression suite passes 205 tests with real PostgreSQL configured. The [synthetic acceptance record](ACCEPTANCE.md) adds installed clients, official native-client compatibility, console mapping, bounded load/soak and interruption/recovery evidence. It does not establish WAN disaster recovery, primary promotion safety, real model capacity or a production support SLO.
