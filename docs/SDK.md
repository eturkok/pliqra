# Install application clients

[← Pliqra](../README.md)

SDK versions are independent of the gateway version. The stable packaged client is **1.0.0** on both registries. SDK 1.x requests stable `pliqra/1.0` and also supports recognized legacy 2.1 servers. Existing 0.1 clients continue to work with gateway 2.2 through the compatibility envelope. The executable profile is `pliqra-core/1`; [normative rules](protocol/1.0/SPEC.md).

## Python

```sh
python -m pip install 'pliqra-client==1.0.0'
export PLIQRA_API_KEY='YOUR_SCOPED_KEY'
```

```python
import os
from pliqra_client import Pliqra

client = Pliqra('http://localhost:8088', os.environ['PLIQRA_API_KEY'])
connection = client.connect('support', ['chat', 'streaming'])
for packet in connection.stream([{'role': 'user', 'content': 'Hello'}], max_tokens=128):
    for choice in packet.get('choices', []):
        print(choice.get('delta', {}).get('content', ''), end='', flush=True)
```

Python 3.10+; standard-library transport and no gateway/runtime dependency. The wheel also installs `pliqra-client` and `pliqra-conformance` commands. For a private CA, use the Python standard-library `SSL_CERT_FILE` trust configuration.

## JavaScript / TypeScript

```sh
npm install @eturkok/pliqra@1.0.0
# The stable default is also available as npm install @eturkok/pliqra.
```

```js
import {Pliqra} from '@eturkok/pliqra';

const client = new Pliqra('http://localhost:8088', process.env.PLIQRA_API_KEY);
const connection = await client.connect('support', {requirements: ['chat', 'streaming']});
for await (const packet of connection.stream([{role: 'user', content: 'Hello'}])) {
  process.stdout.write(packet.choices?.[0]?.delta?.content || '');
}
```

ES modules, Node 22+ and modern browser Fetch; TypeScript declarations are included. Browser applications must protect their scoped key and satisfy the gateway's origin policy. A browser bundle does not make a privileged key safe to disclose. Node private-CA trust uses `NODE_EXTRA_CA_CERTS`.

Stable packages: [PyPI `pliqra-client`](https://pypi.org/project/pliqra-client/1.0.0/) · [npm `@eturkok/pliqra`](https://www.npmjs.com/package/@eturkok/pliqra/v/1.0.0). The publication workflow verifies anonymous downloads against the release artifact hashes; the [stable delivery record](STABLE_2_1.md) links the actual results.

## Without package registries

Each original gateway or independent SDK release carries the tested wheel, source distribution, npm tarball and `SDK-MANIFEST.json`, alongside `SHA256SUMS`. Download from the [SDK 1.0.0 release](https://github.com/eturkok/pliqra/releases/tag/v2.2.0), verify checksums and install locally:

```sh
python -m pip install ./pliqra_client-1.0.0-py3-none-any.whl
npm install ./eturkok-pliqra-1.0.0.tgz
```

The manifest identifies the SDK version, source revision and each artifact's SHA-256. Python source, browser modules and the Go client remain downloadable from the repository or **Settings & API**. The Go module retains its original identity `github.com/etrkk5/pliqra/sdk/go` (the repository now redirects to `eturkok/pliqra`); do not infer a Go v2 module from the gateway's 2.x version.

## Connection behavior

Discover → select service → bind capabilities/extensions → invoke. A changed model or gateway contract raises `ContractChanged` / HTTP 409. The application decides when to rediscover and renew. Clients reject credential-forwarding redirects, invalid local invocation paths, oversized replies and incomplete streams. Organization URLs include `/org/{slug}`. Use a scoped application key rather than an administrator credential.

[Core profile and independent reference implementation](CONFORMANCE.md) · [Native API clients](MODEL_INTERFACES.md) · [First endpoint](GETTING_STARTED.md)

## Maintainer publication

Canonical SDK source lives in `sdk/`. `packaging/` contains metadata, not a second client implementation. `scripts/build_sdks.py` builds the artifacts; `scripts/check_sdk_install.py` installs them outside the source tree without production dependencies. CI uploads tested packages and the release workflow attaches those exact bytes.

Independent client patches use the `sdk-vVERSION` tag and [SDK release workflow](../.github/workflows/sdk-release.yml), without rebuilding or moving a gateway image. The manually dispatched [SDK publishing workflow](../.github/workflows/sdk-publish.yml) fetches a published release, verifies its source revision and hashes, then publishes the same assets. Configure a PyPI trusted publisher for `pliqra-client`, GitHub owner `eturkok`, repository `pliqra`, workflow `sdk-publish.yml`, environment `pypi`. The npm publisher for `@eturkok/pliqra` uses that workflow and environment `npm`; its initial package publication requires the owner's authenticated bootstrap. npm stable versions use `latest`; prereleases use `next`. The channel is validated against the immutable SDK source metadata. Trusted publishing uses short-lived job identity; no permanent registry token belongs in the repository. Never overwrite a published version.

The initial npm artifact was bootstrapped with the owner’s authenticated CLI. Its configured GitHub publisher is now validated; an authenticated duplicate-version attempt confirmed immutable-version rejection. Re-dispatching publication verifies public registry files byte for byte. Matching files complete successfully without another upload; different or incomplete published content fails. Registry access errors are never treated as an absent version. [Publication evidence](ACCEPTANCE.md#published-distribution-acceptance).

Publisher tooling is checked out separately from the immutable SDK release source, allowing delivery fixes to verify earlier release assets. Select the release that originally introduced the SDK version (SDK 1.0.0: `v2.2.0`; SDK 0.1.1: `sdk-v0.1.1`; SDK 0.1.0: `v2.1.0`; SDK 0.1.0-rc.1: `v2.1.0-rc.3`); do not submit rebuilt files under an existing package version. New versions are checked for public availability after upload. The GitHub environment links directly to the published package.

Dispatch the current publisher workflow from `main` while selecting the original SDK asset release:

```sh
gh workflow run sdk-publish.yml --ref main -f release_tag=v2.2.0 -f registry=both
```

The corrected retry workflow completed successfully for both npm and PyPI, verifying existing release bytes and public downloads while skipping redundant uploads. [Successful run](https://github.com/eturkok/pliqra/actions/runs/42402749716) · [Machine-readable retry evidence](evidence/sdk-publication-retry.json).

Registry publication also verifies the canonical PyPI installer index links and their SHA-256 values before a clean `pip install`. Version JSON, file downloads and the installer index can propagate at different times; a bounded wait handles that delay without weakening integrity checks. Both registry jobs then install the exact version from the official registry into an independent consumer environment. Four index regression cases reject stale/incomplete listings, changed hashes and unexpected download hosts.

SDK **1.0.0** is the current stable package. The records linked here describe the earlier 0.1.0/0.1.1 deliveries. Canonical clean installs and public artifact hashes passed in the [final patch verification](https://github.com/eturkok/pliqra/actions/runs/42408692802). [Registry evidence](evidence/stable-sdk-registry.json). Installer visibility retries are bounded to 300 seconds and apply only to a missing new version; trust, authentication and integrity failures stop immediately.

The Go module retains its existing import path for source compatibility: `github.com/etrkk5/pliqra/sdk/go`. Protocol 1.0 sources are tagged `sdk/go/v1.0.0`; the repository itself is now hosted under eturkok.
