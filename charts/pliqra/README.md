# Pliqra Helm chart 0.1.0

Install the prebuilt Pliqra **2.1.0** image on Kubernetes **1.34+**. The default image is pinned by digest. The chart has no bundled database, operator or CRD dependency.

```sh
helm upgrade --install pliqra https://github.com/eturkok/pliqra/releases/download/helm-v0.1.0/pliqra-0.1.0.tgz --namespace pliqra --create-namespace --wait
kubectl -n pliqra port-forward service/pliqra 8088:8080
```

Open http://localhost:8088 and create the administrator before enabling public ingress. The default is one SQLite pod with a retained PVC. Your cluster needs a default StorageClass, or an explicitly configured class/existing claim.

For multiple replicas, use the PostgreSQL profile with an externally managed writable database and existing database/encryption Secrets. SQLite scaling and in-place storage-backend/volume replacement are rejected. The chart includes HTTPS ingress, separate startup/readiness/liveness probes, resource controls, PostgreSQL-only HPA, optional PDB/network policy and a `helm test` hook.

**Upgrades use Recreate and can interrupt traffic.** Back up the database and encryption key first. A Helm rollback does not restore database state or make schema downgrades safe. Uninstall retains a chart-created SQLite PVC. Database HA, ingress controllers, TLS certificates, metrics-server and geographic traffic management remain infrastructure responsibilities.

[Installation, values, upgrade procedure and test scope](https://github.com/eturkok/pliqra/blob/main/docs/KUBERNETES.md). Chart releases include SHA-256 checksums and real-cluster acceptance reports. Helm 4.3.1 and Kubernetes 1.34.11/1.37.0 are the validation matrix; this does not imply certification of every Kubernetes distribution.
