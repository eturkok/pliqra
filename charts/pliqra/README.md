# Pliqra Helm chart 0.4.0

Install the prebuilt Pliqra **2.5.0** image on Kubernetes **1.33+**. The default image is pinned by digest. There is no bundled database, operator or CRD dependency.

Install the versioned release package:

```sh
helm upgrade --install pliqra https://github.com/eturkok/pliqra/releases/download/helm-v0.4.0/pliqra-0.4.0.tgz --namespace pliqra --create-namespace --wait
kubectl -n pliqra port-forward service/pliqra 8088:8080
```

Create the administrator privately before enabling public access. The default is one SQLite pod with a retained PVC and fixed non-root UID/GID 10001. The cluster needs a default StorageClass or an explicitly configured class/existing claim. Multiple replicas use external PostgreSQL and existing database/encryption Secrets.

**OpenShift:** add `--set openshift.enabled=true`, or use the [reviewed values example](https://github.com/eturkok/pliqra/blob/helm-v0.4.0/charts/pliqra/examples/openshift.yaml). The opt-in profile lets restricted-v2 assign UID, fsGroup and SELinux labels to both the application and Helm test Pod; no SCC, anyuid grant or cluster role is installed. Enable `route.enabled` only after private setup, with the matching HTTPS public URL and secure cookies. The Route uses edge termination, the router certificate and the existing ClusterIP Service. Kubernetes's optional HTTPS Ingress remains unchanged; Route and Ingress are mutually exclusive.

The chart includes separate startup/readiness/liveness probes, resource controls, PostgreSQL-only HPA, PDB/network policy, topology spread, authenticated ServiceMonitor/PrometheusRule resources and a health hook. Monitoring requires existing Operator CRDs and a private token Secret. Examples cover OpenShift router/monitoring ingress and restricted-v2 recovery. Model Services can remain cluster-internal; direct access is constrained separately with model-side network policy and authentication.

**Upgrades use Recreate and can interrupt traffic.** Back up the database and encryption key. A Helm rollback does not restore database state or make schema downgrades safe. Uninstall retains chart-created SQLite PVCs. Database HA, router certificates, ingress controllers, metrics-server, storage permission support and geographic traffic management remain infrastructure responsibilities.

[Installation, OpenShift workflow, official references and acceptance boundaries](https://github.com/eturkok/pliqra/blob/main/docs/KUBERNETES.md). [Helm 0.4.0 release](https://github.com/eturkok/pliqra/releases/tag/helm-v0.4.0) includes the exact cluster-tested archive, SHA-256 checksum and all three Kubernetes acceptance reports. Chart and gateway releases remain independently versioned.

Validation uses Helm 4.3.1 and disposable Kubernetes **1.33.12 / 1.34.11 / 1.37.0** clusters. The suite upgrades a digest-pinned **2.4.1** workspace to **2.5.0** and rolls back on unchanged schema **7**, checks both storage profiles, and tests synthetic admission-assigned UIDs with the real image and Restricted Pod Security Admission. The Route resource is checked against a checksum-pinned official OpenShift 4.20 schema. These tests do not run OpenShift SCC admission, CRI-O/SELinux or a router controller; perform the documented OpenShift acceptance in the target cluster.
