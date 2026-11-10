# Inference API boundary

> Current release: Gateway 2.2.0 implements stable [Connection Protocol 1.0](protocol/1.0/SPEC.md), retaining the 0.1 envelope for legacy clients. Draft references in the dated review/migration history below describe earlier releases.

[← Pliqra](../README.md) · [Protocol](PROTOCOL.md) · [Gateway policies](GATEWAYS.md)

Pliqra implements a defined chat-completions subset. Version 1.2 rejects unknown fields and ambiguous generation controls with HTTP 422 before inference. This makes capability, token and permission enforcement explicit; arbitrary provider-native extensions are not silently forwarded.

## Supported request groups

| Group | Fields |
|---|---|
| Address and conversation | model, messages, stream, stream_options.include_usage |
| Output ceiling | Either max_tokens or max_completion_tokens; never both |
| Sampling | temperature, top_p, seed, stop, frequency_penalty, presence_penalty, logit_bias |
| Token probabilities | logprobs, top_logprobs |
| Tool use | tools, tool_choice, parallel_tool_calls; legacy functions/function_call share the same capability and permission checks |
| Other supported controls | user, response_format; n and best_of are limited to 1; best_of=1 is validated locally and not forwarded |
| Attribution | pliqra.project_id / team_id; modelport remains a legacy alias, with canonical attribution taking precedence |

The exact types and numeric bounds are available in /openapi.json and [inference_schema.py](../pliqra/inference_schema.py). Messages have explicit role/content, name, tool-call, refusal and reasoning fields. Multimodal input remains dependent on the selected adapter and declared capabilities. Nested request controls must contain finite JSON numbers.

A gateway key cannot override its project/team attribution. Namespaced, standard and protocol invocation paths all enforce its assignment. Session requests are authorized against the selected project/team.

## Admission and execution

Preparation reads a consistent service and policy snapshot. Admission rechecks the current account/key/session, access, service implementation fingerprint and eligible deployments inside the same transaction as quotas, budget reservation, event creation and round-robin cursor advancement.

- Revoked identity/session: HTTP 401/403.
- Changed service or policy: HTTP 409 before upstream access.
- Deployment readiness changed between preparation and admission: HTTP 503; no model call.
- Request/token/concurrency admission failure: HTTP 429, with project budget failures using HTTP 402.

Request event and gateway-ledger settlement commit together. Partial usage never falls below already reported tokens. Unknown remaining usage is still conservative rather than silently free.

## Response and guardrail boundary

Every completion must contain one valid assistant message before it can be recorded as a successful chat response. Non-finite JSON, missing message structure and unsupported shapes fail with HTTP 502.

With output checks configured, Pliqra first creates a controlled envelope. It delivers locally controlled request/model metadata, supported message data and validated numeric usage. Unchecked provider extensions, logprobs, debug strings and arbitrary usage metadata are excluded. Local text checks preserve protocol roles/type tags and operate on content, refusal/reasoning text and tool data fields.

Input-only checks retain live upstream streaming. Output checks run on a complete response before JSON or compatible buffered SSE delivery. Native streaming is not marked verified by a buffered response.

Provider reads are bounded: discovery at 2 MB, completion/stream at 16 MB and individual SSE/NDJSON lines at 1 MB. Live stream chunks are not aggregated to a large transport buffer. A malformed or oversized started stream produces an explicit error and incomplete record; it is never replayed.

## Schema validation

JSON Schema guardrails and evaluation checks share a limit of two temporary worker processes per server. A deadline includes queue time. Timeout/cancellation kills and reaps the child; it does not leave background workers. The worker does not open the workspace database or inherit provider/encryption-key environment variables.

Definitions are limited to 20 KB and 40 nesting levels. Remote reference retrieval is disabled. Linux workers also have CPU/address-space limits. Schema mismatch is a block; validator failure follows the configured error policy.

## Upgrade compatibility

The 1.2 request boundary is intentionally stricter. Clients using undocumented provider parameters must move to supported controls instead of relying on ignored/forwarded fields.

The implementation revision is part of service/gateway fingerprints. Existing bindings from 1.1 require an explicit rebind; they are not silently upgraded. That release used wire protocol draft 0.1; stable 1.0 is introduced in Gateway 2.2. Existing cached responses are treated as misses and rebuilt with deployment provenance. Accounts, credentials and persisted request history remain in the same schema-3 workspace.

See the 2.0 [model interfaces](MODEL_INTERFACES.md), [workflows](WORKFLOWS.md), [scale topology](PLATFORM.md) and [language clients/conformance](CONFORMANCE.md) for the expanded surfaces. The existing typed chat boundary remains in force.
