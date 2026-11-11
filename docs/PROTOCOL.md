# Pliqra Connection Protocol — 1.0 stable

Pliqra Connection Protocol 1.0 is a stable, versioned application-level contract layered over HTTP and existing inference interfaces. It does not depend on Kubernetes, a particular serving runtime, or a particular provider. No ecosystem-wide standardization is claimed.

The [normative 1.0 specification](protocol/1.0/SPEC.md) and [canonical JSON schemas](protocol/1.0/schema.json) define the frozen core contract and maintenance policy. SDK **1.0.1** selects 1.0 automatically; gateway **2.2.2** supports both 1.0 and the existing compatibility envelope.

## Select 1.0; keep existing clients working

```sh
curl -H 'Pliqra-Protocol: pliqra/1.0' http://localhost:8088/.well-known/pliqra
```

This returns `protocol: pliqra/1.0`, `status: stable`, and specification revision `1.0.0`. An unsupported or duplicate version selection returns **406** before a binding or inference can be created.

An **unversioned** request deliberately keeps `protocol: pliqra/0.1-draft` for already published Python, JavaScript and Go clients that compare that value exactly. It now also advertises `preferred_protocol: pliqra/1.0`, both supported versions and compatibility status. The legacy identifier is not the status of 1.0. Gateway 2.x retains this behavior; removal requires a new product major release and migration guide.

SDK 1.x also recognizes older 2.1 servers that ignore the new header and advertise only the legacy protocol. Unknown future identifiers and inconsistent negotiation fail explicitly. Paths, schema **6**, inference contract **5**, and existing fingerprints/bindings are preserved by this promotion. Normal model/policy changes can still require rebinding.

## Discovery

`GET /.well-known/pliqra` returns the protocol version, authenticated service discovery URI, binding endpoint, and invocation interfaces. It contains no service inventory or secrets.

`GET /protocol/v1/services` requires a scoped bearer access key or a user session. The normative specification defines the core minimum; the gateway adds the extended manifest below. It returns only services available to that identity, including:

- Stable service identity and profile revision.
- Configuration fingerprint and required capabilities.
- Eligible models and declared vs. independently verified capabilities.
- Readiness and a decision explanation for each configured deployment.
- The invocation interface, path, and service model name.

## Bind

`POST /protocol/v1/bindings`:

```json
{
  "service": "document-assistant",
  "requirements": ["chat", "streaming"],
  "ttl_seconds": 3600
}
```

Unsupported capabilities cause an explicit `422` response with eligibility decisions. Successful binding creates a server-side record scoped to the authenticated principal, service revision, fingerprint, requirements, and expiry. A binding is not a credential and cannot be used by another identity.

## Invoke

`POST /protocol/v1/bindings/{id}/invoke` accepts the supported chat-completions request shape with the service name in `model` and the same authentication as discovery. All access checks, key revocation, rate limits, and project budget checks still run.

- Wrong identity or expired binding: `403`.
- Changed service profile or deployment metadata: `409`; discover and bind again.
- No eligible deployment: `422`.
- Revoked or expired access credential: `401`.

The fingerprint covers service identity/version, requirements, enabled state, deployment membership/order, model names, endpoints, adapter types, declared capabilities, enabled states, metadata hashes, pricing, project scope, cache/content/verification policies and request limits. It excludes credentials, runtime health, and newly acquired verification evidence. A binding retains the agreed routing policy across its eligible deployments; it does not promise a single pinned model or equivalent model output quality.

## Execution evidence

Inference responses carry `X-Request-ID` and `X-Pliqra-Model`. Authorized workspace users can inspect metadata and the ordered routing decision record with `/api/v1/events/{id}`. Content capture is disabled by default. A service can opt into encrypted content retention; only workspace administrators can read it, and each read is audited.

Before streaming starts, a failed eligible upstream may be replaced by another eligible upstream according to the declared policy. No fallback occurs after the streaming response has begun. EOF without the expected completion marker is incomplete, not success. Cancellation closes the upstream connection; provider-side compute cancellation is best effort.

## Portability boundary

Portable service exports contain no provider credentials or endpoint addresses. Import requires explicit deployment-reference mapping. The reference implementation and independent Python client below demonstrate a second implementation of discovery/binding/invocation. Provider-specific APIs, identity federation configuration and arbitrary third-party policies are not portable through this core contract.

## Independent client and implementation

The dependency-free client `sdk/pliqra_client.py` can discover, bind, complete and stream. It raises `ContractChanged` for 409 and rejects incomplete streams. It never imports the gateway or forwards credentials across redirects.

```sh
export PLIQRA_API_KEY='your-scoped-key'
python sdk/pliqra_client.py --url http://localhost:8088 conformance --service your-service
python sdk/pliqra_client.py --url http://localhost:8088 chat --service your-service --prompt Hello
```

The conformance command checks discovery, binding and unsupported capability rejection without inference. Chat and streaming are separate billable operations.

`examples/reference_server.py` is a second implementation built only with Python's standard library. Configure `REFERENCE_UPSTREAM` with an OpenAI-compatible base URL ending in `/v1`, `REFERENCE_MODEL`, `REFERENCE_TOKEN`, and optionally `REFERENCE_UPSTREAM_KEY`; it listens on port 8098 by default. Its service is `reference-chat`. It demonstrates the handshake against an actual configured upstream and shares no gateway code, database or authentication state. The 1.0 reference implements text-only chat/streaming and optional `max_tokens`, explicitly rejecting unsupported controls. It is a development reference, not an alternative hardened production gateway.

## Portable configuration

`GET /api/v1/ports/{id}/export` produces `pliqra/service-export/v1`: a service policy plus named deployment references, model names, adapter labels and connection labels. Credentials, endpoint addresses and local project grants are excluded. `POST /api/v1/ports/import` requires mapping every reference to an existing local deployment. Unknown policy fields and unmapped deployments are rejected; destination project access is set explicitly. Native manifests and portable service exports serve different purposes.

Protocol 1.0 has a separate lifecycle from gateway and SDK releases. The [compatibility policy](protocol/1.0/SPEC.md#8-compatibility-and-maintenance-policy) freezes required behavior and reserves incompatible changes for a new wire major version. The independent client has been exercised against both implementations for discovery, bindings, completions, streaming and rejection of unsupported capabilities. This establishes interoperability between these implementations; it does not establish third-party adoption or ecosystem-wide standardization.

## Optional managed-gateway policy

Pliqra gateway keys apply their assigned policy to discovery, plain chat and bound invocation. The discovery document advertises an optional gateway_policy path. GET /protocol/v1/gateway returns the effective credential-free policy contract; bindings include optional gateway_id and gateway_fingerprint fields. A policy or referenced guardrail change invalidates the accepted gateway fingerprint and requires a new binding. Independent implementations may omit these extensions. See [gateway policies](GATEWAYS.md) for accounting and guardrail delivery semantics.

See the 2.0 [model interfaces](MODEL_INTERFACES.md), [workflows](WORKFLOWS.md), [scale topology](PLATFORM.md) and [language clients/conformance](CONFORMANCE.md) for the expanded surfaces. The existing typed chat boundary remains in force.

## Extension negotiation

Discovery advertises optional `pliqra.policy/1`, `pliqra.operations/1`, `pliqra.prompts/1` and `pliqra.batches/1` identifiers. Bind requests accept `required_extensions` and `optional_extensions`; unknown required identifiers return 422, unknown optional identifiers are ignored. Successful responses list the accepted identifiers. Negotiation describes interface expectations; it never grants identity permissions or bypasses policies.

Requirements may additionally include `embeddings`, `audio_speech` and `audio_transcription`; these imply the operations extension. The binding response supplies `operation_invocations` for embeddings, speech and transcription. Existing core chat invocations remain unchanged. Optional implementations may support only the core profile and reject all required extensions. Fingerprints include implementation revision **pliqra-inference/5**; upgrading from an older implementation requires explicit rebinding. Canary publication and policy changes do too.

## Site-aware placement extension

Optional `pliqra.placement/1` advertises model-location policies in the accepted service contract. Discovery includes service `placement` and deployment site/region labels; bindings return the accepted placement policy. Region/site constraints and endpoint-location changes affect the fingerprint. `X-Pliqra-Site` identifies the ingress site and `X-Pliqra-Model-Site` identifies the producing model location. Runtime site traffic mode and observed reachability do not change the contract; they affect current eligibility. The same identity may invoke a binding in another site when its contract allows the resulting model placement. See [multi-DC semantics](MULTI_DC.md).

## Existing Chat Completions clients (2.3 development source)

After creating a binding with the same principal, send its identifier in **X-Pliqra-Binding** to `/v1/chat/completions` or `/g/{slug}/v1/chat/completions`. JSON and streaming use the same authorization, accepted requirements, expiry, service/gateway fingerprints and accounting as the explicit binding invocation. Successful protected responses echo the binding identifier. The header is consumed by Pliqra and is never forwarded to the model.

A missing stored binding, wrong owner or expired binding returns 403. A changed contract returns 409 before model execution. Reconnect explicitly after review. A malformed, empty or duplicated header returns 422. Supplying it on another interface returns 422, preventing unsupported calls from appearing protected. Calls without the header follow their existing behavior.

This is an optional gateway HTTP integration, outside the frozen protocol core. It adds no binding fields, schema migration or SDK requirement. [Client configuration and publication impact](CONTRACT_CHANGES.md).
