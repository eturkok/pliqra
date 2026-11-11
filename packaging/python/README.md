# Pliqra Python client

Dependency-free Python 3.10+ client for the stable Pliqra Connection Protocol 1.0. SDK 1.0.x accompanies the Pliqra 2.2 gateway; SDK versions have their own lifecycle.

```sh
python -m pip install pliqra-client
```

```python
import os
from pliqra_client import Pliqra, ContractChanged

client = Pliqra("http://localhost:8088", os.environ["PLIQRA_API_KEY"])
connection = client.connect("support", ["chat", "streaming"])
for chunk in connection.stream([{"role": "user", "content": "Hello"}]):
    for choice in chunk.get("choices", []):
        print(choice.get("delta", {}).get("content", ""), end="", flush=True)
```

Changed contracts raise `ContractChanged` (HTTP 409); rediscover and bind explicitly. Credentials are not forwarded across redirects. Incomplete streams raise an error. Embeddings/audio operations and durable batches use the same binding. An organization uses its `/org/slug` base URL.

`pliqra-client --help` and `pliqra-conformance --help` expose the included CLIs. Conformance performs no model call unless `--invoke` is supplied. Set `PLIQRA_API_KEY` through your secret environment rather than a command argument.

See [Pliqra](https://github.com/eturkok/pliqra), [protocol](https://github.com/eturkok/pliqra/blob/main/docs/PROTOCOL.md) and [client documentation](https://github.com/eturkok/pliqra/blob/main/docs/CONFORMANCE.md). Existing 0.1 clients remain compatible; a passing core profile is not vendor certification or a guarantee of model quality. Licensed under Apache-2.0.
