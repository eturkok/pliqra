# Pliqra JavaScript / TypeScript client

Dependency-free ESM package for Node 22+ and modern browsers. TypeScript declarations are included. SDK versions have a separate lifecycle from gateway releases.

```sh
npm install @eturkok/pliqra
```

```js
import {Pliqra, ContractChanged} from '@eturkok/pliqra';

const client = new Pliqra('http://localhost:8088', process.env.PLIQRA_API_KEY);
const connection = await client.connect('support', {requirements: ['chat', 'streaming']});
for await (const chunk of connection.stream([{role: 'user', content: 'Hello'}])) {
  process.stdout.write(chunk.choices?.[0]?.delta?.content || '');
}
```

Changed contracts raise `ContractChanged` (HTTP 409). Rediscover/rebind deliberately. Streaming supports cancellation and rejects missing completion markers. The client does not forward credentials across redirects. Organizations use their `/org/slug` base URL. Model operations and batches are also supported.

Use server-side secrets for applications; do not bundle privileged keys into a public browser application. The stable wire protocol is 1.0 with legacy 0.1 compatibility; interoperability does not certify model quality or provider safety. See [Pliqra documentation](https://github.com/eturkok/pliqra/blob/main/docs/CONFORMANCE.md). Apache-2.0 licensed.
