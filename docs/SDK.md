# Install application clients

[← Pliqra](../README.md)

SDK versions are independent of the gateway version. The first packaged candidate is **0.1.0-rc.1** (`0.1.0rc1` on PyPI). The wire protocol remains `pliqra/0.1-draft`; the executable profile is `pliqra-core/1`.

## Python

```sh
python -m pip install 'pliqra-client==0.1.0rc1'
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
npm install @eturkok/pliqra@0.1.0-rc.1
# The prerelease channel is also available as @eturkok/pliqra@next.
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

Published candidates: [PyPI `pliqra-client`](https://pypi.org/project/pliqra-client/0.1.0rc1/) · [npm `@eturkok/pliqra`](https://www.npmjs.com/package/@eturkok/pliqra/v/0.1.0-rc.1). Anonymous registry downloads and clean installs were verified against the release artifact hashes.

## Without package registries

Every gateway release carries the tested wheel, source distribution, npm tarball and `SDK-MANIFEST.json`, alongside `SHA256SUMS`. Download from the [RC3 release](https://github.com/eturkok/pliqra/releases/tag/v2.1.0-rc.3), verify checksums and install locally:

```sh
python -m pip install ./pliqra_client-0.1.0rc1-py3-none-any.whl
npm install ./eturkok-pliqra-0.1.0-rc.1.tgz
```

The manifest identifies the SDK version, source revision and each artifact's SHA-256. Python source, browser modules and the Go client remain downloadable from the repository or **Settings & API**. The Go module retains its original identity `github.com/etrkk5/pliqra/sdk/go` (the repository now redirects to `eturkok/pliqra`); do not infer a Go v2 module from the gateway's 2.x version.

## Connection behavior

Discover → select service → bind capabilities/extensions → invoke. A changed model or gateway contract raises `ContractChanged` / HTTP 409. The application decides when to rediscover and renew. Clients reject credential-forwarding redirects, invalid local invocation paths, oversized replies and incomplete streams. Organization URLs include `/org/{slug}`. Use a scoped application key rather than an administrator credential.

[Core profile and independent reference implementation](CONFORMANCE.md) · [Native API clients](MODEL_INTERFACES.md) · [First endpoint](GETTING_STARTED.md)

## Maintainer publication

Canonical SDK source lives in `sdk/`. `packaging/` contains metadata, not a second client implementation. `scripts/build_sdks.py` builds the artifacts; `scripts/check_sdk_install.py` installs them outside the source tree without production dependencies. CI uploads tested packages and the release workflow attaches those exact bytes.

The manually dispatched [SDK publishing workflow](../.github/workflows/sdk-publish.yml) fetches a published release, verifies its source revision and hashes, then publishes the same assets. Configure a PyPI trusted publisher for `pliqra-client`, GitHub owner `eturkok`, repository `pliqra`, workflow `sdk-publish.yml`, environment `pypi`. The npm publisher for `@eturkok/pliqra` uses that workflow and environment `npm`; its initial package publication requires the owner's authenticated bootstrap. npm prereleases use the `next` tag. Trusted publishing uses short-lived job identity; no permanent registry token belongs in the repository. Never overwrite a published version.

The initial npm artifact was bootstrapped with the owner’s authenticated CLI. Its configured GitHub publisher is now validated; an authenticated duplicate-version attempt confirmed immutable-version rejection. Re-dispatching publication verifies public registry files byte for byte. Matching files complete successfully without another upload; different or incomplete published content fails. Registry access errors are never treated as an absent version. [Publication evidence](ACCEPTANCE.md#published-distribution-acceptance).

Publisher tooling is checked out separately from the immutable SDK release source, allowing delivery fixes to verify earlier release assets. Select the release that originally introduced the SDK version (currently `v2.1.0-rc.3`); do not submit rebuilt files under an existing package version. New versions are checked for public availability after upload. The GitHub environment links directly to the published package.

Dispatch the current publisher workflow from `main` while selecting the original SDK asset release:

```sh
gh workflow run sdk-publish.yml --ref main -f release_tag=v2.1.0-rc.3 -f registry=both
```
