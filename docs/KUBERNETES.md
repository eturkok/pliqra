# Kubernetes, OpenShift and Helm

Pliqra's optional Helm chart packages the same platform-independent application used by Docker Compose. It does not deploy model servers, require OpenShift, install an operator or provision a database cluster.

| Profile | Application state | Replicas |
|---|---|---|
| Default SQLite | One retained PVC, including the generated encryption key | Exactly one; autoscaling is rejected |
| PostgreSQL | One external writable authority and an existing shared encryption Secret; each pod has separate scratch space | One or more; optional CPU HPA |
| Multi-DC | PostgreSQL profile in each site with a common authority/key and distinct site identity | Your shared ingress/database HA infrastructure connects sites |

## Quick start

Use Kubernetes 1.33+ and Helm. The tested Helm version is 4.3.1. A default StorageClass must exist unless you set `persistence.storageClass` or `persistence.existingClaim`.

```sh
git clone --depth 1 https://github.com/eturkok/pliqra.git
cd pliqra
helm upgrade --install pliqra ./charts/pliqra --namespace pliqra --create-namespace --wait
kubectl -n pliqra port-forward service/pliqra 8088:8080
```

Open **http://localhost:8088**, create the administrator, connect a model endpoint reachable from the pods, then publish your service. Model endpoints running on your laptop are not Kubernetes services; `localhost` inside a pod refers to that pod.

The default Service is cluster-internal and ingress is disabled. Initialize the administrator privately before exposing a workspace. `helm test pliqra -n pliqra` checks readiness and protocol discovery through the Service without creating an administrator or making an inference call.

ClusterIP alone does not isolate bootstrap from other cluster tenants. In a shared cluster, restrict access with namespace/network controls **before** first setup, use an authorized port-forward for initialization, and verify untrusted pods cannot reach setup. When using the chart's NetworkPolicy, begin with explicit rules or a deny-all ingress policy and add only intended clients after initialization. Verify your CNI's port-forward behavior; do not temporarily expose public ingress to work around it.

Source chart **0.4.0** pins the published **2.5.0** application image by digest. Chart and application versions have separate lifecycles. To select another image tag deliberately, also clear `image.digest`; a digest takes precedence over a tag.

## Source chart and downloadable packages

The commands above install chart 0.4.0 from source. A separately tagged chart release publishes a checksummed archive; commit/push alone does not publish one. The existing **0.3.1** package below is historical: it pins Gateway **2.2.1** and does **not** contain the new OpenShift profile/Route. For a reproducible source installation, check out your reviewed commit and run `helm package charts/pliqra`; install that exact local archive. See [distribution](DISTRIBUTION.md#kubernetes-chart-distribution).

To verify the older published package:

```sh
curl -fL -o pliqra-0.3.1.tgz https://github.com/eturkok/pliqra/releases/download/helm-v0.3.1/pliqra-0.3.1.tgz
curl -fL -o SHA256SUMS https://github.com/eturkok/pliqra/releases/download/helm-v0.3.1/SHA256SUMS
sha256sum --check SHA256SUMS
helm show values ./pliqra-0.3.1.tgz
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
| `ingress.*` | Optional Kubernetes HTTPS Ingress, existing class and TLS Secret |
| `route.*` | Optional OpenShift edge Route, required host, router certificate and annotations; mutually exclusive with Ingress |
| `openshift.enabled` | Delegate identities/SELinux to restricted-v2 and harden the application and health hook |
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
helm upgrade --install pliqra ./pliqra-0.3.1.tgz --namespace pliqra --create-namespace -f postgresql.yaml --wait
```

Use this profile on a **fresh installation**. Changing `storage.mode` or an existing SQLite data claim in place is rejected. For SQLite-to-PostgreSQL migration, stop the source and follow [offline migration](PLATFORM.md#backup-and-migration), preserving its encryption key, then install a separate release against the restored database. The chart does not migrate data between backends.

The default key is backed by the SQLite PVC when no external encryption Secret is selected. With PostgreSQL, only pod-local scratch space is ephemeral; the shared database and encryption Secret are your durable state. Never attach one SQLite volume to several replicas.

## Failure domains and maintenance

The PostgreSQL example includes **soft hostname spread**. `topologySpreadConstraints` receives the release's pod selector automatically, including a custom release name. For a strict two-zone requirement, use `topology.kubernetes.io/zone`, `DoNotSchedule` and `minDomains: 2`. The cluster must actually have eligible nodes and capacity in those zones; hard constraints can leave pods Pending. Multiple replicas on one node are not node-failure protection. [Kubernetes topology spread](https://kubernetes.io/docs/concepts/scheduling-eviction/topology-spread-constraints/) explains this scheduling boundary.

A PDB's `minAvailable` must be lower than the minimum configured replica count, including the HPA minimum. The chart rejects a configuration that blocks every healthy voluntary eviction. A one-pod SQLite installation normally leaves PDB disabled and accepts a maintenance interruption. `unhealthyPodEvictionPolicy: AlwaysAllow` lets an unhealthy pod be evicted during node maintenance; it does not make healthy-pod eviction bypass the budget. PDBs do not protect against node crashes or the chart's Recreate upgrade. See [the Kubernetes PDB API](https://kubernetes.io/docs/reference/kubernetes-api/policy/pod-disruption-budget-v1/).

After a reviewed database credential or CA update, change a non-secret `config.revision` in your values and run the usual reviewed Helm upgrade. The changed pod annotation replaces the pods; environment-based Secret references and TLS connections are initialized again. This is an explicit rollout trigger, not a Secret watcher or encryption-key rotation. Never replace the master key without a supported data re-encryption/migration procedure. Account for the Recreate interruption and coordinate other releases/sites using that database.

## Monitoring integration

Initialize the administrator privately, create a monitoring token in **Operations**, and deliver it through your secret manager as `pliqra-monitoring/token` in the application namespace. With an existing Prometheus Operator installation, add [monitoring values](../charts/pliqra/examples/monitoring.yaml):

```sh
curl -fL -o monitoring.yaml https://raw.githubusercontent.com/eturkok/pliqra/helm-v0.3.1/charts/pliqra/examples/monitoring.yaml
helm upgrade pliqra ./pliqra-0.3.1.tgz -n pliqra -f your-values.yaml -f monitoring.yaml --wait
```

Review the downloaded example and adapt its labels/Secret reference before upgrading. No source checkout is required.

The optional **ServiceMonitor** scrapes `/metrics` with `authorization.credentials` referencing the Secret; no token is stored in values or rendered resources. Enable its **PrometheusRule** for missing/all-failed scrapes, an individual failed replica and unresolved usage reservations. Both are opt-in and require existing CRDs; the chart fails clearly if they are missing. It installs neither CRDs nor a monitoring stack. Adapt the example labels to your Prometheus `serviceMonitorSelector`, namespace selector and rule selector. Allow the Prometheus pods through NetworkPolicy. [Operator selection and troubleshooting](https://prometheus-operator.dev/docs/platform/troubleshooting/) covers discovery failures.

An HTTP scrape inside the cluster is unencrypted unless your service mesh supplies transport encryption. Keep this path private and restrict ingress to trusted collectors; use your existing encrypted transport policy when required. The existing public ingress is not required for scraping.

See [metric aggregation, alarm response and external availability checks](OPERATIONS.md#monitoring-and-alerts). Scrape success is not proof that a model call succeeds. These rules deliberately avoid deriving request rates from the retained-history counter.

## HTTPS and inference traffic

After private administrator setup, combine your selected storage profile with the [ingress example](../charts/pliqra/examples/ingress.yaml). Provide the existing ingress controller and TLS Secret. Ingress requires `config.publicUrl=https://<host>` and secure cookies.

The example annotations are for NGINX; translate them for your controller. Preserve SSE streaming, allow the expected request duration, disable response buffering where needed, and disable automatic inference retries. A broken connection can follow an already billed model call. The chart does not retry or replay that call. If you enable NetworkPolicy, explicitly allow your ingress controller, intended in-cluster clients and Helm test traffic; the chart's default-deny ingress policy does not infer those sources.

## OpenShift

The chart uses the same `apps/v1` Deployment and ClusterIP Service on both platforms. **Kubernetes 1.33+** is the chart baseline, including [OpenShift 4.20’s Kubernetes 1.33 base](https://docs.redhat.com/en/documentation/openshift_container_platform/4.20/html/release_notes/ocp-4-20-release-notes). OpenShift-specific behavior is explicit and opt-in; ordinary Kubernetes installs do not require OpenShift APIs. The published image supports **linux/amd64 and linux/arm64**; select compatible nodes with `nodeSelector` on mixed-architecture clusters. Other OpenShift architectures need a separately built and accepted image. The chart does not deploy GPU/model servers or manage cluster operators.

### Private installation and restricted-v2

Select a project you are authorized to manage, or create one with `oc new-project pliqra`. For the default SQLite profile, from the repository checkout:

```sh
helm upgrade --install pliqra ./charts/pliqra -n pliqra \
  -f charts/pliqra/examples/openshift.yaml --wait
oc -n pliqra port-forward service/pliqra 8088:8080
```

Create the administrator at **http://localhost:8088**. Route and Ingress are both disabled at this stage. Restrict other tenant pods before bootstrap as described above. For PostgreSQL, add your reviewed PostgreSQL values and existing Secret references to **every** install/upgrade command; the OpenShift profile composes with either storage mode, multi-DC, monitoring, HPA and PDB.

With `openshift.enabled=true`, both the Deployment and Helm health hook omit fixed `runAsUser`, `runAsGroup`, `fsGroup`, supplemental groups and SELinux labels. OpenShift SCC admission supplies the namespace-scoped identity and volume group. The profile enforces non-root execution, RuntimeDefault seccomp, dropped capabilities, no privilege escalation, no privileged container and a read-only root filesystem, even if a values overlay attempts weaker container settings. Fixed identity overrides are intentionally ignored in this profile. It installs **no SCC, anyuid binding or cluster role**. Kubernetes defaults retain UID/GID/fsGroup 10001.

Pliqra listens on unprivileged port **8080**, reads application files from `/app`, and writes only to mounted `/data` and `/tmp`. SQLite needs a PVC whose storage driver supports the SCC-assigned filesystem group and SELinux policy; PostgreSQL uses pod-local scratch volumes. For imported/pre-existing data, review ownership and key-file permissions with your storage administrator before enabling the profile. Namespace identity ranges differ: moving a PVC to another project is a recovery/migration operation, not an ordinary restart. No privileged chmod/chown init container is installed to bypass storage policy.

### Publish one gateway Route

After private setup, enable the Route with a hostname covered by the existing router certificate. For the same default SQLite installation:

```sh
helm upgrade pliqra ./charts/pliqra -n pliqra \
  -f charts/pliqra/examples/openshift.yaml \
  --set route.enabled=true \
  --set-string route.host=pliqra.apps.example.com \
  --set-string config.publicUrl=https://pliqra.apps.example.com \
  --set config.secureCookies=true --wait
helm test pliqra -n pliqra
oc -n pliqra get route pliqra
```

Retain all your reviewed storage, Secret, resource and network values when upgrading; the example above is not a replacement for a customized production values file. Check Route admission, DNS and certificate coverage in your cluster. Route requires the native `route.openshift.io/v1/Route` API, exact HTTPS public URL and secure cookies; Ingress and Route cannot both be enabled. Route can be used independently of the security profile when the platform team manages identity settings another way.

The Route uses **edge TLS termination** and the router's configured certificate; HTTP is redirected to HTTPS by default. Set `route.tls.insecureEdgeTerminationPolicy=None` to reject insecure HTTP instead. The router-to-Pliqra Service connection is HTTP; edge termination does not encrypt that hop. This chart does not support reencrypt/passthrough or inline private keys. Use your platform's supported encrypted transport solution if that hop must be encrypted. The example sets the router timeout to `120s`; review it for actual model request/stream duration. Increasing it does not extend the application's shutdown deadline. Verify incremental SSE delivery, interrupted streams and cancellation; never automatically retry a possibly billed inference call.

Connect an existing model using its reachable HTTP API, for example `http://model-a.models.svc.cluster.local:8000/v1` (adjust cluster DNS and provider API). Applications use `https://pliqra.apps.example.com/v1`, select the published service in `model`, and authenticate with their Pliqra key. Named gateways use `/g/<slug>/v1` on the same Route. Models need no external Route. Registration does not remove existing model Routes or block direct Service access: configure model-side NetworkPolicy/authentication separately to prevent bypassing gateway policies.

### Networking, monitoring, trust and recovery

| Area | OpenShift configuration and operator responsibility |
|---|---|
| Router and collectors | Optional [network values](../charts/pliqra/examples/openshift-networkpolicy.yaml) allow the operator-labelled ingress namespace, the release's health hook and user-workload monitoring namespace to TCP 8080. Combine with the OpenShift profile after reviewing your actual router, release name and intended clients. The empty pod selector alongside namespace selectors accommodates host-networked routers. Do not add operator-reserved labels to application projects. |
| Model/database egress | The chart policy controls **Pliqra ingress only**. Apply your platform's egress controls for DNS, model Services, PostgreSQL, identity providers, guardrails and any external APIs. Permit only the intended gateway namespace/pod selector on the model side; verify unauthorized pods cannot bypass it. |
| Monitoring | Have the platform administrator enable OpenShift user-workload monitoring first. Combine [monitoring values](../charts/pliqra/examples/monitoring.yaml) with the profile, provide the Pliqra monitoring-token Secret in the release namespace, and adapt monitor/rule labels to your collectors. Existing ServiceMonitor/PrometheusRule APIs are required. Verify target discovery, valid-token access, 401 without a token, alert delivery and recovery. The chart does not enable or reconfigure the platform monitoring operator. |
| Credentials and TLS | Deliver PostgreSQL/encryption/monitoring credentials through existing Secrets; use `imagePullSecrets` for private mirrors. Database CA mounts use `database.tls.*` and verified PostgreSQL TLS. For HTTPS model/IdP/guardrail endpoints with private CAs, mount a reviewed complete CA bundle using `extraVolumes`/`extraVolumeMounts` and set `PLIQRA_CA_BUNDLE` through `extraEnv`; retain required public roots as well. Router certificates, service-serving CAs and outbound trust are different responsibilities. Do not disable verification. |
| Health and resources | Startup/liveness use `/livez`; readiness uses `/readyz` on the internal named HTTP port. Set CPU/memory requests and limits to fit project quotas. HPA needs metrics-server/available resource metrics and PostgreSQL. Placement/PDB need actual node/zone capacity; neither makes Recreate upgrades interruption-free. |
| Forwarded identity | If client IPs are needed for sign-in throttling behind the router, configure `PLIQRA_TRUSTED_PROXIES` through `extraEnv` with only the verified router/proxy source CIDRs. Forwarded headers are not trusted by default; do not trust arbitrary clients or the whole internet. Review OpenShift source NAT and chained proxies before choosing the list. |
| Controlled changes | Keep storage, image digest and Secret references in your reviewed values. Bump `config.revision` after a reviewed credential/CA update to replace pods, then verify readiness, login, provider access and metrics. Master-key rotation is a separate migration. |
| Recovery | For isolated SQLite drills, use [the OpenShift recovery Pod](../charts/pliqra/examples/openshift-recovery-pod.yaml), which also delegates identities to restricted-v2. Stop all drill gateway writers, use that release's PVC/key and follow [the recovery procedure](OPERATIONS.md#backup-and-recovery). PostgreSQL backup/HA/PITR stays with your database platform. Verify an authenticated model call after restore, and keep scheduled behavior checks paused until reviewed. |

### Target-cluster acceptance

After installation, inspect admission rather than assuming this profile grants a policy:

```sh
oc -n pliqra get pods -l app.kubernetes.io/instance=pliqra \
  -o 'jsonpath={range .items[*]}{.metadata.name}{"\t"}{.metadata.annotations.openshift\.io/scc}{"\t"}{.spec.containers[0].securityContext.runAsUser}{"\t"}{.spec.securityContext.fsGroup}{"\n"}{end}'
oc -n pliqra get route pliqra -o 'jsonpath={.status.ingress[*].conditions}'
curl --fail --show-error https://pliqra.apps.example.com/readyz
```

Verify the application **and Helm test Pod** are admitted under the intended `restricted-v2` SCC, with allocated non-root IDs and usable volumes. The health hook is removed after success; inspect it while running or through the cluster's retained audit/events. Verify DNS/TLS/Route admission, actual inference and SSE through the Route, user-workload scrape/alerts, positive and denied network paths, a replacement pod retaining encrypted data, controlled Secret/CA rollout, and an isolated restore. A `Ready` pod alone does not prove these integrations work. Real OpenShift admission, CRI-O/SELinux, the selected CSI driver, router and OVN enforcement require this target-cluster check; kind schema/UID simulations cannot certify them.

### Official platform references

- [Helm chart best practices](https://docs.helm.sh/docs/chart_best_practices/) — chart structure, values and managed labels.
- [Kubernetes Restricted Pod Security Standard](https://kubernetes.io/docs/concepts/security/pod-security-standards/) and [security contexts/volume ownership](https://kubernetes.io/docs/tasks/configure-pod-container/security-context/).
- [OpenShift security context constraints](https://docs.redhat.com/en/documentation/openshift_container_platform/4.20/html/authentication_and_authorization/managing-pod-security-policies) and [arbitrary-user image requirements](https://docs.redhat.com/en/documentation/openshift_container_platform/4.20/html/images/creating-images).
- [OpenShift Routes and TLS](https://docs.redhat.com/en/documentation/openshift_container_platform/4.20/html/ingress_and_load_balancing/routes), [network policies](https://docs.redhat.com/en/documentation/openshift_container_platform/4.20/html-single/network_security/index), and [user-workload monitoring](https://docs.redhat.com/en/documentation/monitoring_stack_for_red_hat_openshift/4.20/html-single/configuring_user_workload_monitoring/configuring_user_workload_monitoring).

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

The **Helm chart** workflow lints and renders the chart, rejects unsafe values, tests alert failure/recovery/deduplication with checksum-pinned **promtool 3.16.0**, packages it once, and installs that exact archive in disposable kind clusters on **Kubernetes 1.33.12, 1.34.11 and 1.37.0**. It exercises both storage profiles: administrator setup, encrypted provider access, bound inference, shared PostgreSQL sessions/quotas with verified database TLS, a real 2.4.1-to-2.5.0 SQLite image upgrade and same-schema rollback, PostgreSQL configuration upgrades/rollback, three replicas, retained SQLite PVC and reinstall. It also creates encrypted backups, rejects incorrect passwords, restores into isolated SQLite directories/PostgreSQL schemas, verifies credentials/bindings and session revocation, and checks the source remains usable. Monitoring-token authorization, configuration-revision pod replacement and topology-spread resources are exercised too.

Optional ingress/HPA/PDB/network-policy resources are validated by the Kubernetes API. The OpenShift profile is also rendered and run with simulated admission-assigned UID/fsGroup values under Kubernetes Restricted Pod Security Admission, including the health hook, private model inference, encrypted credentials and retained-PVC restart. The Route is server-validated against the checksum-pinned official OpenShift 4.20 API schema (`openshift/api` commit `b32f49f8a70985c6ad9abb2dc579abc18d143051`) installed **only in disposable kind tests**; this does not install or exercise a router, SCC admission, CRI-O or SELinux. Never install that test CRD on OpenShift, which supplies its native Route API. ServiceMonitor and PrometheusRule resources use the pinned **Prometheus Operator 0.95.1 CRD schemas**. The fixture does not run the Operator/controller, an ingress controller, metrics-server or a policy-enforcing CNI. Alarm expressions are tested with promtool; actual collector discovery, Alertmanager delivery and node/zone loss belong in your deployment acceptance.

The **Publish Helm chart** workflow accepts only a matching `helm-v<chart-version>` tag, reruns the checks, then publishes the exact tested archive, SHA-256 checksum and all three cluster reports as a separate GitHub release. It does not overwrite the application image or stable application release.

These are single-node synthetic cluster exercises. Actual storage drivers, managed-cluster admission policies, geographic failures, database promotion, model capacity and workload-specific availability still need deployment acceptance. No OpenShift/vendor certification, production capacity or zero-downtime guarantee is implied.

Source chart **0.4.0** passed **55 rendering/safety checks**, alert-rule tests and **60 live cluster checks** across the three Kubernetes versions (20 per version: SQLite, PostgreSQL and simulated OpenShift identities). All nine general CI jobs passed. The [source-chart validation record](evidence/helm-0.4.0.json) identifies the exact tested package checksum, source commit and reports; it is not a claim that a `helm-v0.4.0` release archive has been published.

Previously published chart **0.3.1** selects Gateway **2.2.1** and runs the same packaged-chart cluster gates. Its published archive includes both cluster reports; the [current patch delivery record](evidence/patch-2.2.1.json) identifies its exact package checksum and all 32 cluster checks. The earlier [chart 0.3.0 acceptance record](evidence/helm-0.3.0.json) records the exact published package checksum, 35 rendering/safety checks, five alert scenarios (12 assertions), and 32 cluster checks. [Previous 0.2.0](evidence/helm-0.2.0.json) and [0.1.0](evidence/helm-0.1.0.json) records remain available for those earlier releases.
