# Pliqra Connection Protocol 1.0

**Status: stable. Specification revision: 1.0.0. Wire identifier: `pliqra/1.0`.**

This is Pliqra's versioned, application-level HTTP contract. It is independent of Kubernetes, model serving infrastructure and the gateway product's release number. It is maintained by this project; stable does not mean external adoption, independent certification or an industry standard.

This document freezes the **core profile `pliqra-core/1`**. The words **must**, **must not** and **may** express requirements, prohibitions and permitted choices. [Machine-readable schemas](schema.json) describe canonical messages. Additional response properties are permitted and clients must ignore properties they do not understand.

## 1. Version selection and compatibility

An explicit client sends exactly one request header:

```http
Pliqra-Protocol: pliqra/1.0
```

Version selection applies to `/.well-known/pliqra` and `/protocol/v1/` routes, including unsuccessful requests. A server supporting 1.0 must honor this selection; the response header `Pliqra-Protocol` identifies the selected version. These responses must use `Cache-Control: no-store` and include `Pliqra-Protocol` in `Vary`.

An unsupported, empty or duplicated version header must return **406** with `detail.code: unsupported_protocol` and a `detail.supported` array. It must not create a binding, start inference or silently select another protocol. A 406 response does not select a version. Versions are exact identifiers, not floating semantic-version ranges or a comma-separated preference list.

**Pliqra's dual-version compatibility rule:** a request without this header selects the frozen `pliqra/0.1-draft` envelope. This deliberately preserves the exact `protocol` value checked by already published SDKs. Explicit legacy selection also works. Discovery advertises `preferred_protocol: pliqra/1.0`, both supported identifiers and the stable specification revision. A stable-only independent implementation may default to 1.0 instead.

SDK 1.x requests 1.0. It may also connect to older 2.1 servers that ignore the new header and return the recognized legacy identifier without advertising 1.0. It must reject unknown response identifiers and a server advertising 1.0 while ignoring an explicit 1.0 selection. An explicitly rejected version must not trigger an automatic downgrade/retry.

The `/protocol/v1/` URL namespace is unchanged. Version selection is independent of URL spelling, database schema, SDK release numbers and inference implementation revision.

## 2. Workspace base, transport and authentication

All protocol paths are local absolute paths beginning with `/`, never `//`, an external URI or a backslash path. They are appended to the configured workspace base. For example, base `https://ai.example/org/research` plus `/protocol/v1/services` remains within that workspace. Servers must not embed credentials in these paths. Clients must reject external/ambiguous paths and must not forward credentials through redirects.

Remote production deployments use verified HTTPS. Core access uses `Authorization: Bearer <scoped credential>`. The public discovery document requires no credential and must contain no service inventory, user data or secrets. Service discovery, binding and invocation require authentication and authorization. A binding is an agreement identifier, never a credential; possession alone grants no access. Sessions are an optional gateway-specific authentication mechanism subject to its CSRF rules.

## 3. Discovery

`GET /.well-known/pliqra` with explicit 1.0 selection returns:

```json
{
  "protocol": "pliqra/1.0",
  "status": "stable",
  "preferred_protocol": "pliqra/1.0",
  "supported_protocols": ["pliqra/1.0", "pliqra/0.1-draft"],
  "specification_version": "1.0.0",
  "discovery": "/protocol/v1/services",
  "bind": "/protocol/v1/bindings",
  "extensions": {},
  "conformance_profile": "pliqra-core/1"
}
```

The legacy identifier is required in this example's list only because this server supports the compatibility envelope. Core-only implementations may advertise no extensions. `preferred_protocol` must be a supported stable identifier and may name a newer major version; clients use the explicitly selected `protocol`, not that preference, to interpret the response. Specification patch identifiers may advance within 1.0.x without changing wire semantics.

Authenticated `GET <discovery>` returns `protocol` and `services`. Only authorized services may appear. Each service supplies `id`, `name`, integer `version`, opaque `fingerprint`, `requirements`, and `invocation` with `interface`, local `path` and `model`. Core invocation uses `openai-chat-completions`. Fingerprints must change when the accepted service contract changes; their hash algorithm is not a public API.

Readiness, deployments, placement and verified-capability observations may be supplied as additional fields. Declared capabilities must not be presented as independent verification. An empty authorized inventory is valid.

## 4. Binding

`POST <bind>` accepts a JSON object. `service` names the service or its stable local identity. Optional `requirements` defaults to an empty list; `ttl_seconds` defaults to 3600 and canonical requests use an integer from **60 through 86400**. Core clients normally require `chat`; `streaming` requests the streaming capability.

`required_extensions` and `optional_extensions` are string arrays, each with at most 20 entries. Unsupported requirements or required extensions return **422** without creating a binding. Unknown optional extensions are ignored, not reported as accepted. Accepted extension identifiers appear in the response. Negotiation never grants permissions or bypasses gateway policy.

Success is **201** with `id`, `service`, `version`, `fingerprint`, `requirements`, `expires` (Unix seconds), local `invoke` path and `extensions`. The expiration must be in the future. The server must scope the record to the authenticated principal and the accepted service/policy configuration. Clients must not infer capabilities merely from an endpoint's presence.

## 5. Bound invocation

`POST <invoke>` uses the bound service's returned name in `model`, a `messages` array, and optional supported chat parameters. Core messages use the documented [chat API subset](../../INFERENCE_API.md); arbitrary provider-native controls are not made portable by this protocol.

Every call must recheck authentication, principal ownership, binding expiry, current authorization, the accepted fingerprint and applicable admission policies. Validity at binding time does not reserve future capacity. An unavailable deployment does not authorize substitution outside the accepted contract.

| Condition | Required outcome |
|---|---|
| Invalid, expired or revoked credential | 401; no provider call |
| Principal lacks access, binding belongs to another principal, or binding expired | 403; no provider call |
| Binding does not exist | 404; no provider call |
| Bound service name or accepted service/gateway fingerprint changed | 409; no provider call; explicit rediscovery/rebinding |
| Invalid request, unsupported requirement, or no eligible deployment | 422; no provider call |
| Applicable request/token/concurrency or gateway policy-window limit | 429; no admitted provider attempt |
| Configured project budget cannot cover the estimated reservation | 402; no admitted provider attempt |
| Authoritative state unavailable or site not admitting traffic | 503; no new admission |

These are pre-admission boundaries. An unrelated upstream or transport error after admission may follow already billed work. Checks may stop at the first applicable failure; for example an invalid credential is rejected before revealing whether a binding exists. Error bodies use `detail` as a string, object or redacted validation list. Clients must use status codes for these boundaries rather than matching English messages. Missing optional policy features are not implied by passing the core profile.

Version selection alone must not change a service fingerprint or invalidate a live legacy binding. The same principal may invoke an unchanged binding through either supported envelope. Normal configuration, policy or inference-contract changes can still require explicit renewal.

## 6. Completion and streaming

A non-streaming successful core invocation returns JSON with a `choices` array containing assistant output. Streaming uses `Content-Type: text/event-stream`, JSON `data:` events and a terminal `data: [DONE]` event. UTF-8 and events can span arbitrary transport chunks. Clients must not treat a truncated stream or an error event as successful completion. Server/client errors after response headers may require stream termination rather than another HTTP status.

Client cancellation must close its connection; provider compute cancellation remains best effort. An implementation may fall back to another eligible deployment before a stream starts, within the accepted policy. It must not replay or switch a started stream automatically. A broken connection does not prove the provider performed no billable work. Local guardrails requiring complete output may deliberately buffer it.

Provider usage and configured-price cost are execution evidence, not a billing guarantee or protocol-level quality certification. The gateway's request IDs, attempt records and site headers are documented product extensions.

## 7. Optional extensions

Extension identifiers version their own contracts. The gateway supports `pliqra.policy/1`, `pliqra.operations/1`, `pliqra.prompts/1`, `pliqra.batches/1` and `pliqra.placement/1`. A core implementation may omit all five. Their management routes retain their own API versions; this core specification does not turn the whole management API into a portable standard.

Required `embeddings`, `audio_speech` or `audio_transcription` capabilities imply the operations extension in the Pliqra gateway. Clients must use advertised, accepted operation paths and documented subsets. A future feature that is necessary for correct execution must be negotiated; it cannot rely on clients silently ignoring a new property.

## 8. Compatibility and maintenance policy

- **Wire 1.0:** required fields, status boundaries and security/contract semantics above are frozen. Optional fields and negotiated extensions may be added. Incompatible wire semantics require a new major identifier and an explicit migration path; they must not silently replace 1.0.
- **Specification 1.0.x:** editorial corrections or clarifications may be released as specification patches without changing the wire identifier. A correction that changes required behavior is not merely editorial.
- **Gateway 2.x:** Pliqra retains unversioned/explicit `0.1-draft` compatibility throughout this major product line. Removing it requires a new gateway major release and published migration notes. The legacy suffix is retained as an identifier, not a statement that 1.0 remains experimental.
- **SDK 1.x:** the documented public client API and stable 1.0 support are maintained within the major SDK line, including recognized legacy fallback. Removing legacy fallback requires a new SDK major release. Unknown future protocols fail explicitly.
- **Stored state:** this promotion keeps database schema 6 and inference contract `pliqra-inference/5`. It does not by itself require rebinding or rewriting stored agreements. Other configuration changes remain visible contract changes.

See [the implementation and conformance guide](../../CONFORMANCE.md) for independent clients, the separately implemented standard-library reference server, frozen old SDK fixtures and the executable profile. Interoperability between these implementations is evidence of this defined scope, not independent attestation or ecosystem adoption.
