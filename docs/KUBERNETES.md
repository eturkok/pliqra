# Kubernetes and Helm

Pliqra's optional Helm chart packages the same platform-independent application used by Docker Compose. It does not deploy model servers, require OpenShift, install an operator or provision a database cluster.

| Profile | Application state | Replicas |
|---|---|---|
| Default SQLite | One retained PVC, including the generated encryption key | Exactly one; autoscaling is rejected |
| PostgreSQL | One external writable authority and an existing shared encryption Secret; each pod has separate scratch space | One or more; optional CPU HPA |
| Multi-DC | PostgreSQL profile in each site with a common authority/key and distinct site identity | Your shared ingress/database HA infrastructure connects sites |

## Quick start

Use Kubernetes 1.34+ and Helm. The tested Helm version is 4.3.1. A default StorageClass must exist unless you set `persistence.storageClass` or `persistence.existingClaim`.

```sh
helm upgrade --install pliqra https://github.com/eturkok/pliqra/releases/download/helm-v0.1.0/pliqra-0.1.0.tgz --namespace pliqra --create-namespace --wait
kubectl -n pliqra port-forward service/pliqra 8088:8080
```

Open **http://localhost:8088**, create the administrator, connect a model endpoint reachable from the pods, then publish your service. Model endpoints running on your laptop are not Kubernetes services; `localhost` inside a pod refers to that pod.

The default Service is cluster-internal and ingress is disabled. Initialize the administrator privately before exposing a workspace. `helm test pliqra -n pliqra` checks readiness and protocol discovery through the Service without creating an administrator or making an inference call.

Chart **0.1.0** pins the published **2.1.0** application image by digest. Chart and application versions have separate lifecycles. To select another image tag deliberately, also clear `image.digest`; a digest takes precedence over a tag.

## Verify the downloadable package

```sh
curl -fL -o pliqra-0.1.0.tgz https://github.com/eturkok/pliqra/releases/download/helm-v0.1.0/pliqra-0.1.0.tgz
curl -fL -o SHA256SUMS https://github.com/eturkok/pliqra/releases/download/helm-v0.1.0/SHA256SUMS
sha256sum --check SHA256SUMS
helm show values ./pliqra-0.1.0.tgz
```

On macOS, use `shasum -a 256 -c SHA256SUMS` instead. The public GitHub release archive needs no registry login; it can also be mirrored into your own Helm/OCI repository. A checksum detects changed bytes; it is not a separate signature or third-party attestation.

## Configure through values

Pass `-f your-values.yaml` to install/upgrade. [Default values](../charts/pliqra/values.yaml) have a JSON Schema, and templates reject unsafe combinations.

| Setting | Purpose |
|---|---|
| `persistence.size`, `storageClass`, `existingClaim` | SQLite PVC capacity/class or an existing claim; class and size are cluster capabilities |
| `storage.mode` | `sqlite` or `postgresql`; immutable on an existing release |
| `database.existingSecret`, `urlKey` | Existing Secret/key containing the psycopg database connection string |
| `encryption.existingSecret`, `key` | Existing Secret/key containing the same Fernet key for every PostgreSQL replica |
| `database.tls.existingSecret`, `caKey` | Mount a database CA as `/run/pliqra/database/ca.crt` |
| `config.publicUrl`, `secureCookies` | Canonical external origin and browser cookie policy |
| `ingress.*` | Existing ingress class, host, TLS Secret and controller annotations |
| `resources`, `nodeSelector`, `affinity`, `tolerations` | Scheduling and resource limits |
| `autoscaling.*` | PostgreSQL-only CPU HPA; requires metrics-server and a CPU request |
| `podDisruptionBudget.*` | Optional voluntary-disruption protection; not protection against every outage |
| `networkPolicy.*` | Optional ingress policy; an empty rule list denies inbound connections. Requires a policy-enforcing CNI; egress remains cluster-managed |
| `podSecurityContext`, `securityContext` | Non-root defaults, read-only root filesystem and dropped capabilities; adapt IDs to your distribution's admission policy |
| `extraEnv`, `extraVolumes`, `extraVolumeMounts` | Additional application settings and mounted files; managed environment variables and reserved mounts cannot be overridden |

No provider credentials, database password or generated encryption key is embedded in Helm values. Reference Secrets managed by your existing secret-delivery system. The chart does not create or silently rotate the master key. External Secret changes need a deliberate pod restart; environment references do not refresh inside an already running process.

## PostgreSQL and multiple replicas

Prepare a writable PostgreSQL endpoint, a database CA Secret and a backend Secret in the `pliqra` namespace. The backend Secret contains `database-url` and `encryption-key`. Use one stable Fernet key, generated once and backed up. For example, its value can be generated with Python's `base64.urlsafe_b64encode(os.urandom(32))`; keep generated secrets out of Git and command logs.

The database URL is a libpq connection string such as:

```text
host=db.example.com port=5432 dbname=pliqra user=pliqra password=YOUR_PASSWORD sslmode=verify-full sslrootcert=/run/pliqra/database/ca.crt target_session_attrs=read-write
```

The configured role needs schema creation/migration permissions. Supply actual credentials through your secret manager or private files with `kubectl create secret generic ... --from-file`, not committed YAML. Then use the [PostgreSQL values example](../charts/pliqra/examples/postgresql.yaml):

```sh
helm upgrade --install pliqra ./pliqra-0.1.0.tgz --namespace pliqra --create-namespace -f postgresql.yaml --wait
```

Use this profile on a **fresh installation**. Changing `storage.mode` or an existing SQLite data claim in place is rejected. For SQLite-to-PostgreSQL migration, stop the source and follow [offline migration](PLATFORM.md#backup-and-migration), preserving its encryption key, then install a separate release against the restored database. The chart does not migrate data between backends.

The default key is backed by the SQLite PVC when no external encryption Secret is selected. With PostgreSQL, only pod-local scratch space is ephemeral; the shared database and encryption Secret are your durable state. Never attach one SQLite volume to several replicas. The optional anti-affinity example uses release name `pliqra`; adapt its instance label if you choose another name.

## HTTPS and inference traffic

After private administrator setup, combine your selected storage profile with the [ingress example](../charts/pliqra/examples/ingress.yaml). Provide the existing ingress controller and TLS Secret. Ingress requires `config.publicUrl=https://<host>` and secure cookies.

The example annotations are for NGINX; translate them for your controller. Preserve SSE streaming, allow the expected request duration, disable response buffering where needed, and disable automatic inference retries. A broken connection can follow an already billed model call. The chart does not retry or replay that call. If you enable NetworkPolicy, explicitly allow your ingress controller, intended in-cluster clients and Helm test traffic; the chart's default-deny ingress policy does not infer those sources.

## Multi-DC

Combine the PostgreSQL profile with [multi-DC values](../charts/pliqra/examples/multidc.yaml), using a distinct site in each cluster. All sites must reach the **same writable PostgreSQL authority** and use the same encryption key. Use verified database TLS. The application rejects remote multi-DC database connections without `sslmode=verify-full`.

The chart installs a site; it does not provide global ingress, PostgreSQL replication/promotion, split-brain fencing or offline quotas. See [multi-DC behavior and infrastructure responsibilities](MULTI_DC.md).

## Upgrade, rollback and uninstall

1. Back up the database and encryption key; verify that your recovery procedure works.
2. Review the target application's schema and compatibility notes. For shared databases across releases/sites, stop **all** older writers before migrating; a chart controls only its own Deployment.
3. Run `helm upgrade` with your reviewed values and `--wait`. The Deployment uses **Recreate**: old pods terminate before the replacement version starts and performs the application's serialized startup migration. Plan an interruption; HPA/PDB settings do not make this a zero-downtime schema upgrade.
4. Run `helm test`, inspect readiness, and make an authenticated call through your configured model endpoint.

A same-image Helm rollback can restore chart configuration. An image downgrade is only safe when the application's database schema remains compatible; `helm rollback` does not restore database contents. Restore an appropriate pre-upgrade backup when required. Do not rotate the encryption key as an upgrade shortcut.

`helm uninstall pliqra -n pliqra` retains a chart-created SQLite PVC. Keep the same release/namespace for a retained-volume reinstall. Existing PVCs and Secrets are never owned by this chart. Deleting the namespace or PVC explicitly can delete durable data, depending on your storage platform; make that a separate operator decision.

## Validation and release pipeline

The **Helm chart** workflow lints and renders the chart, rejects unsafe values, packages it once, and installs that exact archive in disposable kind clusters on **Kubernetes 1.34.11 and 1.37.0**. It exercises both storage profiles: administrator setup, encrypted provider access, bound inference, shared PostgreSQL sessions/quotas with verified database TLS, a real RC4-to-2.1.0 SQLite image upgrade and same-schema rollback, PostgreSQL configuration upgrades/rollback, three replicas, retained SQLite PVC and reinstall. Optional ingress/HPA/PDB/network-policy resources are validated by the Kubernetes API; an ingress controller, metrics-server and policy-enforcing CNI are not part of that test fixture.

The **Publish Helm chart** workflow accepts only a matching `helm-v<chart-version>` tag, reruns the checks, then publishes the exact tested archive, SHA-256 checksum and both cluster reports as a separate GitHub release. It does not overwrite the application image or stable application release.

These are single-node synthetic cluster exercises. Actual storage drivers, managed-cluster admission policies, geographic failures, database promotion, model capacity and workload-specific availability still need deployment acceptance. No OpenShift/vendor certification, production capacity or zero-downtime guarantee is implied.
