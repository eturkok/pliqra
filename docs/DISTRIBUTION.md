# Releases and prebuilt images

[← Pliqra](../README.md)

## Install the stable release

**2.1.0 is the stable community release for the [documented support envelope](SUPPORT.md).** The version does not imply a capacity, availability or vendor certification. Docker with Compose is required. Linux AMD64 and ARM64 images are published at `ghcr.io/eturkok/pliqra:2.1.0`; Docker Desktop on macOS/Windows runs the matching Linux image.

No source checkout, local build or registry login is required:

```sh
mkdir pliqra && cd pliqra
curl -fL -o compose.release.yaml https://github.com/eturkok/pliqra/releases/download/v2.1.0/compose.release.yaml
docker compose -f compose.release.yaml pull
docker compose -f compose.release.yaml up -d
```

Open **http://localhost:8088**, create the administrator and connect a reachable model endpoint. The initial workspace is empty. Port 8088 is bound to loopback; [configure HTTPS](OPERATIONS.md#https-and-storage) before remote exposure. For a model running on the host, use `http://host.docker.internal:PORT`.

The named `pliqra-data` volume holds the workspace and local encryption key. To stop, run `docker compose -f compose.release.yaml down`; preserve the volume to retain data. Source-build and prebuilt Compose use the same workspace volume: stop the old container first and never run both against it. Test the selected release with a separate workspace before upgrading existing data.

## Pin and verify

The [GitHub release](https://github.com/eturkok/pliqra/releases/tag/v2.1.0) includes:

- `compose.release.yaml`: installation using the exact version tag, without `build`.
- `image.txt`: the exact multi-platform OCI digest reference.
- `release.json`: source commit, image digest, platforms, schema and protocol revisions.
- `SHA256SUMS`: SHA-256 checksums of the attached installation/metadata files.
- `DISTRIBUTION.md`: this guide.
- `acceptance-evidence.tar.gz`: exact-image synthetic load/soak, installed/official clients and upgrade/recovery reports.
- `dependency-audit.json`: dated runtime Python dependency advisory results, without suppressions.
- `SDK-MANIFEST.json`, Python wheel/source distribution and npm tarball: tested standalone clients. [SDK installation →](SDK.md)

Download `SHA256SUMS` and the files it lists to one directory, then run `sha256sum -c SHA256SUMS` (macOS: `shasum -a 256 -c SHA256SUMS`). Checksums detect changed downloads; they are not an independent publisher signature. Pin the published digest when reproducibility matters:

```sh
export PLIQRA_IMAGE="$(cat image.txt)"
docker compose -f compose.release.yaml pull
docker compose -f compose.release.yaml up -d
```

SBOM and BuildKit build-provenance attestations are attached to the OCI image for each platform. They describe contents/build inputs; they do not certify vulnerability absence. View them with `docker buildx imagetools inspect "$PLIQRA_IMAGE" --format '{{json .SBOM}}'` or `--format '{{json .Provenance}}'`.

## Publication pipeline

The [release workflow](../.github/workflows/release.yml) runs on a `vMAJOR.MINOR.PATCH` or `vMAJOR.MINOR.PATCH-{alpha,beta,rc}.N` tag:

1. Validate the tag against the application version, release notes and prebuilt Compose default.
2. Run the existing Python 3.13/3.14, real PostgreSQL, Docker and JavaScript/Go checks at that tag's commit.
3. Build/push Linux AMD64 + ARM64 with pinned action revisions, locked Python dependencies, OCI source/revision labels, SBOM and provenance. Only the image job receives package-write permission.
4. Pull the image by digest on native AMD64 and ARM64 runners. Check labels, non-root execution, health/readiness, protocol discovery, empty setup, console and anonymous-management rejection.
5. Exercise the exact image and tested SDKs in the synthetic customer topology. Stable releases enforce a 30-minute bounded soak and the committed latency/error/memory thresholds; upgrade and encrypted-backup recovery start from a digest-pinned previous published image. Collect the public reports from that same run.
6. Verify the version tag resolves anonymously to that exact multi-platform digest. Only then publish the GitHub release and installation assets.

RCs are marked **prerelease**, never GitHub's latest release and never given a Docker `latest` tag. Exact version tags are not overwritten by this workflow. Registry administrators can still change tags; digest pinning identifies the exact image. Even stable builds use exact image versions rather than a mutable Docker `latest` alias.

GitHub initially creates a new container package as private. Its owner must set the **pliqra package** to public once in GitHub's package settings; repository visibility alone is insufficient. The anonymous-access gate prevents a public release from advertising an inaccessible image. A failure at this gate can be retried with **Re-run failed jobs** after setting package visibility. Do not rebuild or move a published tag; correct code with a new version/RC instead. No personal registry token is stored: publishing uses the job-scoped `GITHUB_TOKEN`.

Maintainers prepare the version, `docs/releases/vVERSION.md` and Compose references, commit/push, and then create/push the matching annotated tag. The tag-triggered workflow performs publication. Do not manually publish a release ahead of its image and acceptance checks. SDK publication is separate: after the release, dispatch `sdk-publish.yml` for the original SDK asset tag. Stable SDKs publish to npm `latest`; prereleases use `next`. Registry bytes must match the release.

## Upgrade and recover

Back up persistent data **and the encryption key**, stop the previous deployment, then pull/start the selected exact version. This release uses **schema 6**, inference contract revision **5** and protocol draft **0.1**. Upgrade every shared-state replica together. Bindings from an earlier inference contract revision require renewal; unchanged RC4 bindings are preserved by 2.1.0. See [multi-DC upgrade](MULTI_DC.md#evidence-and-upgrade).

Returning to an older image may require restoring its pre-upgrade backup: a newer database is not automatically downgraded. Keep the old image digest and an independently tested backup. [Operating limits](RELEASE.md) and [recovery procedures](OPERATIONS.md) still apply.

## Kubernetes chart distribution

The official [Helm chart](KUBERNETES.md) is released independently as `helm-v<chart-version>`. Version 0.1.0 uses the existing 2.1.0 application image by digest. The chart workflow packages once and installs that exact archive in disposable Kubernetes clusters before publishing it with SHA-256 checksums and both cluster reports. The chart release does not replace or retag the application image; the latest application release remains 2.1.0. [Chart source](../charts/pliqra/README.md).
