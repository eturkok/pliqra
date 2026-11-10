# Storage, replicas and organizations

The default remains `docker compose up --build -d`: one image, SQLite, no external control plane. Use the optional topology when multiple gateway processes need shared policies and limits.

## Start two gateways

```sh
./scripts/start-enterprise.sh
```

This generates `.env.enterprise` once, preserves it on later runs, and starts PostgreSQL 18, two gateways and Caddy on **http://localhost:8088**. The generated file contains the database password and shared encryption key: keep it private and back it up. Do not regenerate it for an existing database. The default installation must be stopped first if it already uses port 8088. Override `PLIQRA_HTTP_PORT` in `.env.enterprise` to choose another port.

```sh
docker compose --env-file .env.enterprise -f compose.enterprise.yaml ps
docker compose --env-file .env.enterprise -f compose.enterprise.yaml stop
```

This is a runnable scale topology, not a replicated database or a production availability guarantee. Put your HTTPS ingress in front, set `PLIQRA_PUBLIC_URL` to its exact public origin and `PLIQRA_SECURE_COOKIES=1`. Never expose the initial setup before creating its administrator. Pin tested image digests in your deployment. Manage database backups, failover and capacity through your database platform.

## Bring your database

Set `PLIQRA_DATABASE_URL` to a psycopg PostgreSQL connection string, `PLIQRA_DATABASE_SCHEMA` (default `pliqra`) to the root workspace schema, and the **same** `PLIQRA_ENCRYPTION_KEY` on every replica. `PLIQRA_ENCRYPTION_KEY_FILE` can read it from a mounted file. Do not share SQLite volumes between processes. Replica-local `/data` directories may be separate with PostgreSQL. Schema creation/migration requires a database role with the corresponding permissions; use a dedicated database/role. For remote PostgreSQL use `sslmode=verify-full` and an appropriate CA.

Workspace writes acquire a schema-specific PostgreSQL advisory transaction lock. Admission, quotas, budget reservations, routing cursors and configuration publication are serialized within that workspace; model calls happen outside the lock. This deliberately favors correctness over unlimited write throughput. Measure your own workload before setting capacity or latency SLOs.

Heartbeats run every five seconds; a 60-second lease expiry fences a replica. Expired requests become interrupted and uncertain usage remains reserved. A recovered fenced process must restart. Queued batch items can resume; started calls are never automatically replayed. Database connection loss does not replay the failing operation; an operational storage failure fences the affected process so uncertain in-flight work can be recovered by lease expiry. Restart that replica after resolving the database problem. Synchronize host clocks. Health checks exclude fenced replicas. Caddy retries are disabled; clients must not blindly retry inference after a disconnect.

## Organizations

**Advanced tools → Workspace & identity → Organizations**, or `POST /api/v1/organizations`, creates a workspace at `/org/{slug}/`. Supply `name`, `slug`, `owner_email` and `owner_name`; the initial owner password is shown once. Each workspace has separate accounts, credentials, projects, policies, history and sessions. SQLite uses separate directories; PostgreSQL uses separate schemas. Keys are derived from the root master key and organization identity. Pausing an organization prevents further admission on every replica.

A root administrator manages the organization directory. Root and organization accounts do not inherit each other's access. An administrator has full visibility **within their workspace**, including team filters and audited captured-content access. Separate schemas/directories are application boundaries; use separate instances/databases for independent infrastructure trust domains. Up to 50 organizations per root are supported.

## Analytics storage

Configure a separate PostgreSQL target in **Workspace & identity → Analytics export**, or `PUT /api/v1/analytics`. `dsn` accepts a connection string or secret reference; remote targets require verified TLS. The exporter upserts metadata by workspace/request identity and source update time, including later usage reconciliation. Raw prompts, responses, binary audio and credentials are excluded. Exports are asynchronous, observable and independent from inference success. `POST /api/v1/analytics/export` triggers an immediate run. This is a reporting copy; retention/deletion at the source does not delete the analytics copy. Apply its own retention and access policy.

## Backup and migration

Each organization is a separate backup/recovery unit; a root archive does not bundle child workspaces. Preserve the master key and organization directory metadata along with every child backup. For large installations use coordinated native database and secret backups.

The console exports password-encrypted physical SQLite snapshots or logical PostgreSQL archives. The online SQLite database limit and logical plaintext archive limit are **32 MB**; use native backup beyond them. Restore refuses active shared inference, management operations, evaluations and queued/running batches, and invalidates sessions. An externally managed/derived key must match the archive. Logical restore is transactional and requires the same schema version and encryption key.

To migrate SQLite to PostgreSQL: stop the source, run `python -m pliqra backup --logical --file /data/workspace.mpb`, retain its encryption key, configure the destination with that same key, then use offline `restore` before starting any destination replica. Never point old and new storage backends at live traffic simultaneously. The schema version in 2.1 is 6. Take a pre-upgrade backup; roll back using that backup and the old image. Changing database format alone is not a downgrade path.

See [operations](OPERATIONS.md), [architecture](ARCHITECTURE.md) and [release evidence](RELEASE.md).

## Multiple data centers

[Active/active multi-DC deployment](MULTI_DC.md) adds site/region identity, per-site endpoint health and circuits, model placement policies, draining/standby modes, readiness/liveness probes and optional fresh-process restart after fencing. It requires the same writable PostgreSQL authority at every site. The two-replica Compose example is a local scale topology; use `compose.multidc.yaml` separately in each actual DC.
