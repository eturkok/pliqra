# Clients and protocol conformance

The stable protocol is **pliqra/1.0**, specification revision **1.0.0**. Gateway 2.2 retains **pliqra/0.1-draft** as an explicit/unversioned compatibility envelope. The independently executable profile is **pliqra-core/1**. This is a maintained project contract, without an industry-standard or independent-certification claim. [Normative specification and lifecycle](protocol/1.0/SPEC.md).

| Client | Files | Capabilities |
|---|---|---|
| Python, standard library | `sdk/pliqra_client.py` | Discovery, bind, complete/stream, operations, batches |
| JavaScript, Node 22+ / modern browser | `sdk/javascript/pliqra.mjs` | Fetch transport, aborts, Unicode SSE, extension/409 handling, operations, batches |
| TypeScript | `sdk/javascript/pliqra.d.mts` | Types for the same JavaScript client |
| Go 1.23+ | `sdk/go` | Context-aware discovery, bindings, completions, streaming and model operations |

[Install Python/JavaScript packages →](SDK.md)

Files are also downloadable in **Settings & API**. All clients reject credential-forwarding redirects and non-local invocation paths, enforce response bounds and detect incomplete streams. A changed contract raises a specific error; applications decide when to rediscover/rebind, rather than silently accepting new policy.

```python
from pliqra_client import Pliqra
client = Pliqra('http://localhost:8088', 'YOUR_SCOPED_KEY')
connection = client.connect('support', ['chat'], required_extensions=['pliqra.prompts/1'])
print(connection.complete([{'role':'user','content':'Hello'}], max_tokens=128))
```

```js
import {Pliqra} from '@eturkok/pliqra';
const client = new Pliqra('http://localhost:8088', process.env.PLIQRA_API_KEY);
const connection = await client.connect('support', {requirements:['chat']});
for await (const part of connection.stream([{role:'user',content:'Hello'}])) {
  process.stdout.write(part.choices?.[0]?.delta?.content || '');
}
```

Organization base URLs include `/org/{slug}`. Keys and bindings remain scoped to that workspace. See [protocol](PROTOCOL.md) for extension identifiers and wire shapes.

## Run the profile

```sh
export PLIQRA_API_KEY='YOUR_SCOPED_KEY'
pliqra-conformance --url http://localhost:8088 --service support
# Adds completion + streaming calls; these can incur provider charges:
pliqra-conformance --url http://localhost:8088 --service support --invoke
```

For negotiated 1.0, eleven metadata/negative checks cover discovery, service contracts, explicit binding, unknown capabilities, required/optional extensions, invalid credentials, explicit/unknown version selection, invalid TTL, missing bindings and wrong bound service names. `--invoke` adds completion and terminal-stream checks. JSON output includes individual pass/fail results, scope and inference flag; exit status fails when any check fails. The suite imports no gateway code. Automated live HTTP tests run it against Pliqra and the separate standard-library reference server with synthetic upstreams. A passing core profile does not certify quality, policy effectiveness, resilience, every optional extension or external adoption.

Run `node sdk/javascript/test.mjs`, `cd sdk/go && go test ./...`, and `python -m pytest tests/test_live_conformance.py -q`. CI runs these checks too. Third parties can use the suite against an independently implemented server; no external attestation has been obtained.

## Compatibility evidence

`tests/compat/sdk011` freezes the unmodified Python, JavaScript and Go SDK sources from `sdk-v0.1.1`, plus the old independent reference server, with source commit and SHA-256 hashes. These client sources also match those bundled with gateway 2.1.0. They perform real HTTP discovery, binding, completion and streaming against both new implementations. New clients also execute those paths against the frozen legacy server. CI requires Node and Go, while a local run explicitly skips a missing language runtime.

Stateful tests cover binding ownership, expiry, revoked credentials, contract changes, version-independent fingerprints/invocation, unsupported-version rejection before side effects, duplicate headers and mounted workspace paths. Canonical gateway messages are checked against the versioned JSON schema. All three SDKs reject unknown protocols and inconsistent negotiation; Python/JavaScript do not cache a rejected discovery response.

The CLI reports its actual negotiated `protocol` and `stability`. Against an old server it runs the legacy seven-check core (nine with inference) and reports **compatibility**, not stable 1.0. A 1.0 server runs eleven checks (thirteen with inference). Conformance does not create an ecosystem-adoption or external-vendor claim.

[Published 1.0 acceptance record](evidence/protocol-1.0.json) identifies the immutable gateway/SDK source, compatibility matrix, release image and exact-image upgrade/recovery run.
