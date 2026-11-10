# Managed gateways and guardrails

Pliqra combines a model-as-a-service catalog with an inference gateway. Connect models that are already running, publish stable services, and create a named gateway for an application, team or workload. A gateway does not provision GPUs or deploy model weights.

Everything in this guide runs in the same Pliqra container. External guardrail engines are optional services that the administrator connects explicitly.

## Administrator workflow

1. Connect a model endpoint and verify its service in the playground.
2. Open **Guardrails** under Advanced tools. Add reusable local rules, a NeMo check endpoint or a custom HTTP decision endpoint.
3. Open **Gateways → Create gateway**. Select services, project/team access, quota rules and ordered guardrail steps.
4. Use **Preview** to inspect routing, quota admission and response delivery without inference or external guardrail calls. This is an administrator preview, not a test of a particular application key.
5. Use **Try** for a real playground request through that gateway, or explicitly test a check from the guardrail library.
6. Use **Issue key**. The new credential is restricted to the gateway and its currently published services, with its own RPM limit and expiry. Copy the credential once.

A gateway address looks like **/g/finance/v1**. Use it as an OpenAI-compatible base URL. An assigned key also enforces its gateway policy on the standard chat endpoint and protocol binding/invocation paths. Switching addresses cannot remove that assignment. A key assigned to another gateway is rejected.

Existing keys without a gateway assignment keep their previous service/project policies. Administrators can issue managed keys from Gateways; assigning policy does not retroactively change other keys.

## What an administrator can compose

| Control | Behavior |
|---|---|
| Service catalog | Explicit list of existing stable services; their priority/fallback or round-robin deployment routing remains active |
| Access | Optional project restriction and allowed teams; service access, key allow-lists and normal authorization also apply |
| Quotas | Up to 30 simultaneous request/token rules, each with an independent rolling window from 1 second to 30 days |
| Quota scope | Whole gateway, key, key-owner/user, project, team or service |
| Concurrency | Maximum in-flight model/check requests for this gateway; project and workspace limits also apply |
| Inference limits | Input character ceiling, output token ceiling, verified-capability routing, tool and image permissions |
| Guardrail chain | Up to 20 ordered steps, independently selecting input/output/both, enforce/observe and stop/allow on provider error |
| Lifecycle | Version history, rollback as a new version, cloning and credential-free definition export/import with local reference mapping |
| Visibility | Last-24-hour gateway admission counters, filtered retained usage/CSV, per-request policy fingerprints and guardrail decisions |

All limits are additive. A permissive gateway cannot enlarge a service limit, key RPM limit or project budget. Project monthly USD budgets use configured model prices and the existing budget reservation mechanism.

**User quota** groups session calls by the signed-in user and application calls by the key creator. It is not an inferred downstream end-user identity. Missing key/project/team attribution has its own shared null bucket within a gateway.

An imported definition contains local service and guardrail references, not provider secrets. The UI opens the editor to map those references before publication. The API accepts a validated name, address name and complete configuration.

Gateway rollback restores gateway settings and check references as a new version. Reusable guardrails keep their current definitions; edit those resources separately when their behavior must also change.

## Inference flow

~~~mermaid
sequenceDiagram
    participant Client
    participant Gateway as Named gateway / assigned key
    participant Admission as Atomic admission
    participant Input as Input checks
    participant Model as Existing model service
    participant Output as Output checks
    participant Ledger as Usage and decision records
    Client->>Gateway: Chat request or bound invocation
    Gateway->>Gateway: Identity, service access and contract checks
    Gateway->>Admission: Reserve request, tokens, concurrency and project budget
    Admission-->>Gateway: Admit or reject before a check/model call
    Gateway->>Input: Ordered checks using admitted policy snapshot
    alt Input blocked or fail-closed provider error
        Input-->>Client: Explicit 422 or 503 with request ID
        Gateway->>Ledger: Decision, zero inference usage, release token/budget reservation
    else Input accepted
        Gateway->>Model: Redacted/accepted messages
        Model-->>Gateway: Completion or upstream stream
        opt Output checks configured
            Gateway->>Output: Full completion before client delivery
            Output-->>Gateway: Pass, redact or block
        end
        Gateway->>Ledger: All model attempts, reported usage, decisions and policy fingerprint
        Gateway-->>Client: Checked JSON / buffered SSE, or live SSE with input-only checks
    end
~~~

## Token and request accounting

Admission is atomic inside the existing SQLite transaction, including concurrent callers. The durable gateway ledger is independent of report filters and is retained for the maximum 30-day quota window.

- Every admitted request consumes a request count, including a guardrail block. Requests rejected by admission do not consume another admission count.
- Pending calls reserve serialized input UTF-8 bytes, 1,024 overhead units, and requested maximum output tokens, multiplied by the number of possible model attempts.
- Complete reported input/output usage settles that reservation across all attempts. This is an admission estimate, not a provider tokenizer or an absolute bound for every modality.
- Partial, missing or interrupted usage keeps the conservative reservation for the remaining window. A restart releases concurrency, not uncertain token charges.
- An input block with no model attempt settles to zero model tokens and releases model-budget reservation. An output block still records and charges model computation that already happened.
- Administrators can reconcile unknown usage through the existing audited accounting dialog. Supplying both token totals also corrects the gateway ledger.

Remote guardrail provider calls may incur separate charges. Those provider charges are not included in model-token or estimated model-cost totals. Rolling windows use request admission time, not calendar-day resets. SQLite policies apply in one workspace process. PostgreSQL replicas share admission counters and reservations through one writable authority; see [multi-DC behavior](MULTI_DC.md).

## Reusable guardrails

| Provider | Supported checks |
|---|---|
| Built in | Case-insensitive literal phrases; approximate email/phone patterns; block or text replacement; output JSON Schema with local references |
| NVIDIA NeMo | Decision-only input/output validation through a configured **POST /v1/checks** endpoint and configuration ID |
| Custom HTTP | Provider-neutral input/output decision webhook; use a small adapter when a vendor has another API shape |

Checks execute in order at each stage. Enforce mode applies a block or local redaction; observe mode records the verdict and leaves the message unchanged. Errors stop the request by default. **Allow and record** is an explicit per-step alternative. An unavailable/disabled check also follows that error policy.

Provider changes are versioned and change every referencing gateway fingerprint. Already-admitted requests keep their guardrail configuration snapshot across asynchronous checks. New requests use the new configuration.

Local pattern matching is not a general DLP engine, semantic safety guarantee or complete PII detector. Local redaction traverses message string values, including text in tool-call arguments. JSON Schema checks apply to output message content; they do not run arbitrary validation code or fetch remote schema references.

**Streaming:** input-only checks preserve live upstream streaming. Any configured output check uses a non-streaming upstream completion, runs the full output chain, then emits compatible SSE to streaming clients. Unchecked partial output is never forwarded. This increases first-content latency. Request details and the X-Pliqra-Delivery header distinguish guarded_buffer from live_stream. Output-checked calls do not mark native upstream streaming as verified.

Gateway-managed calls bypass the existing exact response cache so checks and usage policies always run. Existing direct service caching remains available.

Version 1.2 hardens the [inference request/response boundary](INFERENCE_API.md): unknown generation controls are rejected, legacy tool requests share tool permissions, and output checks receive a controlled envelope. Provider debug/logprobs and arbitrary metadata are excluded from checked delivery. Local phrase checks do not match or rewrite protocol role/type fields. JSON Schema execution uses shared, bounded temporary workers with deadlines instead of running unbounded work on the server loop.

### NeMo compatibility

Configure the full endpoint, for example **http://nemo-guardrails:8000/v1/checks**, and its server-side configuration ID. Authentication header/prefix, encrypted key and deadline are configurable.

~~~json
{
  "messages": [{"role": "user", "content": "A sample question"}],
  "guardrails": {
    "config_id": "company-policy",
    "rail_types": ["input"]
  }
}
~~~

Pliqra accepts a **passed** or **blocked** status. A **modified** result is not silently treated as passed: mapping a single returned string onto arbitrary multipart/tool messages would be ambiguous, so it follows the step's error policy. Use local replacement rules or a decision adapter where appropriate.

This requires a NeMo server implementing the check API. Servers exposing only chat completions require an adapter or an upgrade. Configure and validate the intended NeMo version and policy before production use. Pliqra does not bundle the NeMo runtime, model weights, Colang policy editor or GPU services. The adapter is covered by controlled contract fixtures; a real NeMo deployment was not available for the release acceptance test.

See NVIDIA's [check API guide](https://docs.nvidia.com/nemo/guardrails/latest/run-guardrailed-inference/using-python-apis/check-messages) and [server schema](https://github.com/NVIDIA-NeMo/Guardrails/blob/develop/nemoguardrails/server/schemas/openai.py).

### Custom decision webhook

~~~json
{
  "protocol": "pliqra/guardrail-check/1",
  "stage": "input",
  "messages": [{"role": "user", "content": "A sample question"}]
}
~~~

Return an object with a boolean **allow**, either true or false. A string such as "true", invalid JSON, timeout, redirect or non-success HTTP status is an error. Replies are limited to 1 MB and the configured deadline covers the entire check.

Pliqra sends the messages selected for that stage. The webhook does not receive the application key or model provider credential. Authentication uses the separate configured guardrail credential. Provider response text and checked content are excluded from decision metadata.

[The reference webhook](../examples/guardrail_webhook.py) uses Python's standard library:

~~~sh
BLOCKED_PHRASES="synthetic restricted phrase" python examples/guardrail_webhook.py --port 8100
~~~

When Pliqra runs in Docker and this example runs on the host, configure **http://host.docker.internal:8100/check** on Docker Desktop. The example is a demonstration phrase validator, not a production semantic safety model.

Per-service content capture remains explicit and encrypted. If enabled, it can retain the original submitted request before local redaction. Turn it off when original content must not be retained.

## API and contracts

| Operation | Endpoint |
|---|---|
| List/create gateway | GET / POST /api/v1/gateways |
| Publish complete policy | PUT /api/v1/gateways/{id} |
| Preview policy | POST /api/v1/gateways/{id}/preview |
| Versions / restore | GET /api/v1/gateways/{id}/versions; POST /api/v1/gateways/{id}/rollback/{version} |
| Export definition | GET /api/v1/gateways/{id}/export |
| Issue managed key | POST /api/v1/keys with gateway_id, project_id and optional team_id |
| List/create/edit checks | GET / POST /api/v1/guardrails; PUT /api/v1/guardrails/{id} |
| Explicit check test | POST /api/v1/guardrails/{id}/test |
| Decisions | GET /api/v1/guardrail-decisions; scoped per-request details include their checks |
| Managed inference | GET /g/{slug}/v1/models; POST /g/{slug}/v1/chat/completions |
| Effective policy contract | GET /protocol/v1/gateway; keys resolve their assignment automatically |
| Gateway-filtered reports | Existing usage, events and CSV routes accept gateway_id |

All configuration and provider-test routes require a signed-in administrator. Application keys cannot use management APIs. The effective protocol policy excludes provider endpoints and secrets.

Bindings now retain gateway ID and fingerprint alongside the service contract. A gateway or referenced guardrail change causes an old invocation to return **409 before model inference**. Clients explicitly bind again; stale paths are not silently upgraded. The overview includes gateway policy changes in renewal counts.

Existing standalone protocol implementations can keep omitting the optional gateway fields. The protocol remains draft 0.1; these product extensions do not imply ecosystem standardization.

## Console examples

These screenshots use an isolated synthetic workspace. The NeMo form illustrates configuration; its example provider was left disabled.

![Gateway quotas and an ordered guardrail chain](screenshots/gateway-builder.jpg)

![Guardrail providers and recorded verdicts](screenshots/guardrails.jpg)

![NeMo check endpoint configuration](screenshots/nemo-configuration.jpg)

![Gateway-scoped credential creation before any secret is shown](screenshots/gateway-key.jpg)

![Per-request gateway fingerprint and guardrail decisions](screenshots/gateway-request.jpg)
