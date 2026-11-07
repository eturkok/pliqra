# Pliqra product website

English product presentation for [Pliqra](https://github.com/eturkok/pliqra). Canonical address: **https://pliqra.etlabio.com**.

## Develop

Use Node 24.15+ and npm 12.2+. The application uses the supplied Vinext/Vite Sites integration.

```sh
npm ci
npm run dev
npm run build
```

The default development address is http://localhost:5173. Production output is in `dist/`; `.openai/hosting.json` links the native Sites deployment. Keep that project identity when maintaining the existing site.

## Experience

- Eight interactive browser-only scenarios: contracts, gateway request/token quotas, guardrails, team usage, active/active routing, reviewed fleet policy, evidence-gated canaries and identity/access.
- Actual console screenshots use synthetic workspaces and are explicitly labelled as screenshots.
- No model calls, API credentials, sign-up form, third-party trackers or customer data are used by the application.
- Ten capability areas link to the actual product guides; topology choices explain SQLite, shared PostgreSQL and independent fleet boundaries.
- Installation commands and Python/JavaScript application code are copied separately.
- Server-rendered content, canonical/Open Graph metadata, structured data, sitemap, robots file and a local PNG social image support discovery.

## Content maintenance

Gateway and SDK versions are explicit. Update release URLs, commands, evidence dates and compatibility copy together. Synthetic acceptance measurements are historical regression results, not capacity promises. Never invent customer logos, vendor certifications or protocol adoption.

Source: `app/page.tsx`, `components/product-demo.tsx`, `app/globals.css`; static assets in `public/`. The demo is a teaching simulation, not a remotely accessible Pliqra administrator console.
