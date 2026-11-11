# Operate and recover Pliqra

[← Pliqra](../README.md)

Configuration, persistent data, recovery and release boundaries for a self-hosted workspace.

For current prebuilt images, digest verification and release upgrades, see [distribution](DISTRIBUTION.md). Versions 2.1 and 2.2 use schema 6 and inference contract revision 5; follow [the current multi-DC upgrade procedure](MULTI_DC.md#evidence-and-upgrade) for shared deployments.

## Enterprise operations review

Reviewed on **2026-11-10**, following the first Helm release. The application remains **2.1.0 / schema 6**; chart **0.2.0** adds deployment and operations controls without changing the application image.

| Area | Finding and current treatment |
|---|---|
| Monitoring | Added opt-in authenticated ServiceMonitor and tested PrometheusRule alerts. Collector selection, alert delivery and an external model-call check must be accepted in the target environment. |
| Replica placement | Added release-scoped hostname/zone spread. PDB validation rejects a budget that prevents all healthy voluntary evictions; unhealthy-pod policy is explicit. |
| Secret changes | Added a non-secret configuration revision to trigger a reviewed pod replacement. No implicit master-key rotation or cluster-wide Secret access. |
| Recovery | Expanded cluster acceptance to actual isolated physical SQLite and logical PostgreSQL restores, incorrect-password rejection, encrypted credentials, bindings and session revocation. The recovery procedure below covers operator use. |
| Upgrade safety | Already uses Recreate, serialized migration, persistent key validation and storage-change guards. Old writers in other releases/sites must still be stopped. |
| Supply chain | Existing immutable image digest, SBOM/provenance and separate checksummed, cluster-tested chart releases. Mirror all required artifacts for disconnected installations. |
| Infrastructure ownership | Database PITR/promotion/fencing, off-site backup retention, ingress/certificates, egress policy, storage drivers, node/zone capacity and alert routing remain explicit operator responsibilities. |

These controls do not certify a particular cluster or establish a production SLA. Set workload-specific recovery objectives and perform the deployment acceptance below before carrying production traffic.

## HTTPS and storage

Persist the whole `/data` volume. It contains SQLite/WAL data and `encryption.key`; neither alone is a complete recovery copy. Provider secrets, SAML private keys, optional captured content and cached responses use authenticated encryption. Keys and sessions are stored as hashes; passwords use salted scrypt. Startup refuses a missing/mismatched encryption key and a second process sharing the same volume.

| Variable | Default | Purpose |
|---|---|---|
| `PLIQRA_DATA_DIR` | `data` locally; `/data` in Docker | Persistent directory |
| `PLIQRA_HOST` / `PLIQRA_PORT` | `0.0.0.0` / `8080` | Listener |
| `PLIQRA_PUBLIC_URL` | Request base URL | Exact public origin for redirects, CSRF and federation |
| `PLIQRA_SECURE_COOKIES` | `0` | Set `1` for HTTPS |
| `PLIQRA_ENCRYPTION_KEY` | Generated persistent key | Optional externally managed Fernet key; preserve it |
| `PLIQRA_DATABASE_URL` / `PLIQRA_DATABASE_SCHEMA` | Unset / `pliqra` | Optional PostgreSQL storage and schema |
| `PLIQRA_ENCRYPTION_KEY_FILE` | Unset | Mounted shared master key; required with PostgreSQL unless supplied directly |
| `PLIQRA_CA_BUNDLE` | System trust | Custom CA bundle for outbound HTTP |
| `PLIQRA_SECRET_ROOT` | `/run/secrets` | Boundary for mounted-file secret aliases |
| `PLIQRA_TLS_CERTFILE` / `PLIQRA_TLS_KEYFILE` | Unset | Native HTTPS certificate and private-key file paths |

HTTPS can use the native TLS settings with mounted certificate files, or your existing reverse proxy. Configure the exact public URL and secure cookies. Forwarded headers are not trusted automatically. The default loopback port mapping is intended for local setup; create the administrator before exposing the instance to a wider network.

## Backup and recovery

In **Operations**, download a password-encrypted backup and keep its password separately. It includes a consistent database snapshot and its encryption key. Restore requires typing `RESTORE`, no active shared operations or pending batches, and signs everyone out. Physical SQLite online backups are limited to a 32 MB database; PostgreSQL uses a logical archive limited to 32 MB plaintext. Use native backups beyond these limits. See [organization recovery and migration](PLATFORM.md#backup-and-migration). A local pre-restore snapshot and crash marker recover an interrupted restore. When `PLIQRA_ENCRYPTION_KEY` is externally supplied, it must match the archive's key.

The following offline commands work for the Compose installation. Stop the service first; the temporary recovery container automatically uses the same volume and non-root user. Passwords are entered interactively:

```sh
docker compose stop pliqra
docker compose run --rm --no-deps pliqra python -m pliqra check
docker compose run --rm --no-deps pliqra python -m pliqra backup --file /data/workspace.mpb
docker compose run --rm --no-deps pliqra python -m pliqra restore --file /data/workspace.mpb
docker compose run --rm --no-deps pliqra python -m pliqra reset-password --email owner@example.com
docker compose up -d pliqra
```

Change an account's own password in **Settings & API**. Administrators can revoke member sessions and rotate application, provisioning and monitoring tokens. At least one active local administrator must remain.

### Recovery objectives and scheduled backups

Assign a recovery owner and record **RPO** (acceptable data loss) and **RTO** (time to restore service). The software does not select these for you. For example, a one-hour RPO requires a successful protected backup at least hourly, including detection/retry of failed jobs; a daily backup cannot meet it. Measure RTO by restoring and making a real authenticated model call, not by timing a file download.

For PostgreSQL, use your database platform's scheduled base backups/WAL archiving and point-in-time recovery. Preserve the matching master key separately, plus the organization directory and every organization schema. Test recovery into an independent database. A root portable archive does not include child organizations. For SQLite, the online encrypted archive is consistent within its documented size limit; for larger workspaces stop the writer and take a consistent snapshot of the **whole** data volume, including the key. Do not copy only a live `.sqlite3` file and omit its WAL.

Store backups outside the source PVC/cluster, encrypt them, restrict restore access, and monitor the age of the **last successful** backup and available capacity. Use your database/backup platform's scheduler and alerts. The Helm chart does not ship a CronJob that races an active SQLite writer or pretends to provide off-site retention/PITR. Test failures and expiration of the backup credentials as well as successful runs. Keep passwords out of command arguments, Git, logs and support bundles.

### Isolated Kubernetes recovery drill

Use a fresh namespace and a fresh PVC/database; never use the production namespace for a rehearsal. Keep external ingress disabled and isolate egress so a restored batch, identity integration, fleet controller or webhook cannot act on production systems. An archive may contain queued work and credentials. Do not start its gateway until you have reviewed/disabled queued jobs and outbound integrations offline. Recovery verification can remain offline.

For a default SQLite installation, download an encrypted archive in **Operations**, keep its password separately, and use the same compatible image/schema:

1. Install a separate release named `pliqra` in a new `pliqra-drill` namespace with the default chart values, no HPA and no ingress. Scale `deployment/pliqra` there to zero and wait for its application pods to be deleted. Pause any GitOps reconciliation that would recreate writers.
2. Apply [the SQLite recovery pod](../charts/pliqra/examples/recovery-pod.yaml) **in that namespace**. It mounts only the drill PVC, runs non-root and starts no gateway. If your source uses an external encryption key, mount the matching key and set `PLIQRA_ENCRYPTION_KEY_FILE` on this pod before restoring. Adapt the UID/storage settings to your cluster.
3. Copy the encrypted archive, run the offline restore and integrity/encryption check. Enter the archive password and `RESTORE` interactively:

```sh
curl -fL -o recovery-pod.yaml https://raw.githubusercontent.com/eturkok/pliqra/helm-v0.2.0/charts/pliqra/examples/recovery-pod.yaml
kubectl -n pliqra-drill apply -f ./recovery-pod.yaml
kubectl -n pliqra-drill wait --for=condition=Ready pod/pliqra-recovery --timeout=180s
kubectl -n pliqra-drill cp ./workspace.mpb pliqra-recovery:/tmp/workspace.mpb
kubectl -n pliqra-drill exec -it pliqra-recovery -- python -m pliqra restore --file /tmp/workspace.mpb
kubectl -n pliqra-drill exec -it pliqra-recovery -- python -m pliqra check
```

4. Verify the expected administrator, services/bindings, encrypted credential access and revoked sessions without printing secret values. After offline review and network isolation, delete the recovery pod, start only the drill gateway, sign in and perform a deliberately selected test call. Record the backup timestamp, restored schema/image, elapsed time and result. Dispose of the drill namespace and its retained volumes explicitly when finished; check your storage platform's reclaim policy.

For PostgreSQL, restore a native backup into a separate database, or restore a compatible portable **logical** archive using a maintenance container with pod-local scratch space, that destination's connection Secret and the **matching** master key. Stop every destination replica before offline restore and wait for node leases to expire (60 seconds); never point the maintenance container at the live authority. PostgreSQL archives are not restored into SQLite. Follow [organization backup boundaries and offline migration](PLATFORM.md#backup-and-migration).

Repeat after meaningful upgrade, storage/backup-policy or key-delivery changes. CI's small synthetic restore is a regression check, not evidence of your off-site copy, storage driver, geographic recovery or measured RPO/RTO.

## Monitoring and alerts

Create a dedicated monitoring token in **Operations**; `/metrics` is not public and an application key is not a monitoring token. The [Helm monitoring profile](KUBERNETES.md#monitoring-integration) references a pre-existing Secret. A Prometheus configuration without the Operator can instead scrape each pod with `authorization.credentials_file` pointing to its privately mounted token file. Use a trusted internal transport and restrict collector access.

| Signal | Scope and response |
|---|---|
| `up` / `PliqraMetricsUnavailable` | Missing targets or all scrapes failing for five minutes. Check Prometheus selection, token/Secret, network policy, pods and the writable database. A missing monitor can look like a service fault; inspect discovery first. |
| `PliqraReplicaScrapeFailed` | One target fails for five minutes; inspect that replica even if another succeeds. |
| `pliqra_unresolved_costs` / `PliqraUnresolvedCosts` | Shared workspace reservations, not a per-pod total. Use **max**, not sum, across replicas of that workspace. Review Operations and reconcile unknown charges with an audit note. |
| `pliqra_active_requests` | Local in-flight requests; sum across the selected workspace's pods for current activity. |
| `pliqra_requests_total{status}` | Count derived from retained workspace history. It is duplicated on shared replicas and can decrease after retention/restore. Do not sum replicas or treat it as a durable lifetime counter for `rate()`/billing/SLO calculations. |

The supplied rules deduplicate shared reservations for one release. When several sites/releases share a workspace, aggregate/deduplicate at that workspace boundary in your monitoring configuration; independent fleet members have separate data. The root workspace scrape does not automatically cover child organizations. Arrange their authorized monitoring separately.

Use external HTTPS/readiness checks and a low-cost, scoped **synthetic authenticated inference call** with known pricing to check the real user path; a metrics scrape alone does not exercise TLS ingress, routing, guardrails or a model. Set the frequency within provider quotas and label/route those test requests deliberately. Add infrastructure alerts for restart/OOM/Pending pods, PVC space, database replication/availability, certificate expiry and backup age. These rely on your existing cluster/database exporters and certificate/backup systems.

In deployment acceptance, verify the collector discovers **every** replica, no token returns 401, a valid token succeeds, the intended Alertmanager receiver gets a test notification, and a resolved condition clears. The chart tests rule syntax and failure/recovery/deduplication with promtool, validates pinned CRD schemas and checks live token authorization; it does not claim your alert receiver or Operator selectors are configured.

## Change control and deployment acceptance

Keep reviewed Helm values, desired image digest and a non-secret configuration revision in Git; deliver credentials through your secret system. Disable direct imperative drift or reconcile it back into that source. Initialize the administrator privately, configure the canonical HTTPS origin/secure cookies, test OIDC/SAML login **and logout**, preserve a restricted local recovery administrator, then expose ingress. Restrict ingress and provider/database egress with your cluster's network controls; the optional chart policy controls ingress only.

Before a change, take and verify a recovery copy, record the image/chart/schema versions, pause new admissions and queued background jobs, and coordinate all writers sharing the authority. Draining admits no new inference but does not by itself stop every administrative or background writer. Recreate and PDB are not a substitute for this coordination. The gateway's current graceful shutdown allows **30 seconds** for in-flight requests after shutdown begins; the chart adds a five-second pre-stop delay. A longer termination grace period or ingress timeout alone does not extend that application deadline. Plan for interrupted long streams and reconcile uncertain usage; do not replay a possibly billed request automatically.

Before production acceptance, demonstrate these outcomes in the target environment:

- A fresh private installation, secret delivery, first administrator, HTTPS, identity login/logout and an application call all work without exposing bootstrap publicly.
- Every desired replica is placed in the intended failure domain; a failed pod/node and a planned drain behave as expected. Strict zone constraints have enough eligible capacity.
- A database disconnection stops admission, preserves global quotas and recovers without replay. Database promotion/fencing is exercised by your HA platform, not simulated by creating another independent writer.
- Backup jobs run on schedule, failures alert, an off-site copy restores into isolation, and its measured recovery time/data loss meet the recorded objectives.
- An image/chart upgrade and same-schema configuration rollback work; an incompatible image downgrade uses the matching pre-upgrade backup. A secret/CA change uses a reviewed `config.revision` rollout. A master-key change is a separate supported migration, never an arbitrary Secret replacement.
- Monitoring sees each replica, alarms reach an owner, and a bounded workload test establishes capacity for your actual model latency and concurrency.

For disconnected installations, mirror the image **and its platform manifests/verification artifacts**, the Helm archive/checksum, and the images your own ingress/database/monitoring stacks need. Set `image.repository` to the mirror and preserve the selected digest if it is unchanged; render and verify locally before installing. No public registry access is needed at runtime once all chosen artifacts are available internally.

## Usage and operating limits

Costs are configured-price **estimates**, not invoices. Each attempted upstream call is accounted separately. Missing provider usage or pricing remains unknown, including partially known fallback totals. Budgeted calls require pricing and atomically reserve a conservative allowance for possible attempts. Unknown charges keep the remaining reservation until an administrator reconciles them, with an audit note. Budgets use UTC calendar months and do not guarantee final provider billing. Cached calls use zero upstream billable tokens; the returned cached response retains its original provider usage and includes `pliqra.cache_hit`.

Runtime retention defaults to 90 days and cannot be shorter than 32 days, preserving current-month budgets. Pending requests and unresolved reservations are retained. Captured content follows its request's retention; test sets and explicitly saved evaluation outputs remain until deleted. Prometheus `/metrics` uses a separate monitoring bearer token created in **Operations**.

The default is one SQLite workspace/server process per volume. Optional [PostgreSQL topology](PLATFORM.md) supports shared-state replicas, distributed admission and independent organizations. [Model interfaces](MODEL_INTERFACES.md) describes the chat/native/embedding/audio boundaries. Local patterns are not a complete DLP or semantic safety engine; NeMo requires a compatible external check deployment.

## Upgrade an earlier Model Port installation

Pliqra is the new name across the application, Python package, SDK, environment variables, response headers and protocol discovery. The canonical entry points are `python -m pliqra`, `sdk/pliqra_client.py`, `PLIQRA_*`, `X-Pliqra-*` and `/.well-known/pliqra`.

Before upgrading, take an encrypted backup, stop the old container, and attach its existing data volume to Pliqra. The database is checkpointed and atomically renamed from `modelport.sqlite3` to `pliqra.sqlite3`; accounts, services, keys, history and encrypted credentials are retained. An ambiguous volume containing both database names is rejected. Keep the encryption key with its volume.

Existing encrypted backups and portable service exports remain importable. Existing hashed application keys remain valid. Browser sessions should sign in again after the rename. Legacy `MODELPORT_*` environment variables are accepted as a fallback where the corresponding canonical variable is not set; update deployment settings to `PLIQRA_*` explicitly, including Docker/Compose environment values.

For an existing standalone installation using the old volume name:

```sh
docker stop modelport
docker run -d --name pliqra \
  -p 127.0.0.1:8088:8080 \
  -v modelport-data:/data \
  --add-host host.docker.internal:host-gateway \
  --restart unless-stopped pliqra:2.3.0
```

The volume name is an existing storage reference, not the product name. Do not run the old and new processes on the same volume simultaneously. Restore the pre-upgrade backup before returning to the old build.

## Multi-DC operation

For site identity, verified remote database TLS, multi-host writer selection, traffic drain, liveness/readiness and opt-in supervised restart after fencing, follow the [multi-DC guide](MULTI_DC.md). Site and model-location filters apply to totals, request history and CSV export. Preserve one writable authority and a common master key; independently writable database copies cannot enforce global quotas.
