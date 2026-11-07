# 2.1.0 stable readiness

[← Pliqra](../README.md)

This review converts the RC4 baseline into a gated stable release. The [support envelope](SUPPORT.md) distinguishes stable implementation behavior from actual customer/vendor and geographic deployment acceptance.

## Gaps found and resolved

| RC4 gap | Stable-release change | Required evidence |
|---|---|---|
| Customer acceptance was manually dispatched and could be omitted from publication | Reusable acceptance is a dependency of release publication and runs the exact built image digest with the tested SDK assets | Successful `acceptance` job and public report archive |
| Ten-minute initial soak without release-specific pass thresholds | Committed 30-minute synthetic policy: zero failed calls, ≥8,000 calls, p95 ≤2 s and RSS growth ≤64 MiB | `report.json`, including image revision, load/soak observations and gate result |
| Fresh-image smoke without a real published-image upgrade/recovery exercise | Digest-pinned RC4 seeds an isolated workspace; the candidate preserves identities, encrypted endpoint credentials, keys, history, contracts and consumed quota; previous encrypted backup is restored on both versions | `upgrade.json`; exclusively owned containers, network and volumes removed |
| Gateway release could bundle prerelease SDKs; npm publication always used `next` | Bundled SDK 0.1.0 and standalone documentation patch 0.1.1, consistent Python/npm metadata, stable channel `latest`, prerelease channel `next`; stable gateway metadata rejects prerelease SDKs | Package install/compatibility jobs; public registry bytes verified after separate OIDC publication |
| No recurring release dependency-advisory gate | `pip-audit` runs on the locked runtime graph, including resolved dependencies, without suppressions | `dependency-audit.json`; a scan error or finding blocks the release |
| Support and upgrade responsibilities were scattered | Explicit maintained line, compatibility and tested deployment envelope; security reporting policy | [Support](SUPPORT.md), [security](../SECURITY.md), [distribution](DISTRIBUTION.md) |

## Evidence and publication

The tag-triggered [release workflow](../.github/workflows/release.yml) must pass Python 3.13/3.14, real PostgreSQL, JavaScript/Go, clean SDK installation/compatibility, dependency audit, native AMD64/ARM64 image smoke, synthetic acceptance and upgrade/recovery **before** it creates a public stable release. Acceptance artifacts are collected from the same run; the release checks their `passed` flags and attaches their exact archive with checksums.

The [2.1.0 release](https://github.com/eturkok/pliqra/releases/tag/v2.1.0) carries `release.json`, `acceptance-evidence.tar.gz`, `dependency-audit.json`, SDK artifacts and `SHA256SUMS`. Their immutable source commit/digest identify the tested build. SDK registry publication subsequently reuses the release's already-tested bytes, checks public availability and cannot overwrite an existing version. A registry failure is surfaced as a separate delivery failure, never hidden as gateway acceptance.

The [RC4 console evidence](RESPONSIVE_UI.md) covers the unchanged console runtime. Older [RC3 customer results](ACCEPTANCE.md) remain historical; they are not relabelled as measurements of 2.1.0.

## External acceptance remains explicit

No real customer usability feedback, third-party protocol attestation, provider billing certification, geographic WAN partition or PostgreSQL primary-promotion result is claimed. Thirty minutes is a bounded synthetic regression exercise, not a multi-day endurance test. These deployment-specific limits are documented in the stable support envelope; the release cannot manufacture outside participants or their approval.

SDK 0.1.1 corrects the package README candidate-install examples without changing client implementation or moving the 2.1.0 gateway tag. It has its own `sdk-v0.1.1` tested release assets and registry publication. The gateway's bundled SDK 0.1.0 remains immutable.

## Verified stable delivery — 7 November 2026

[2.1.0 is published as the latest stable gateway release](https://github.com/eturkok/pliqra/releases/tag/v2.1.0). All jobs in the [tagged release run](https://github.com/eturkok/pliqra/actions/runs/42405015917) succeeded, including both native architecture checks, the exact-image synthetic gate and owned-fixture cleanup. Source: `8a6fdf4abe7ff4dc3afba71281de3ce9cec7f136`; public AMD64/ARM64 digest: `sha256:3b46ae2608b3b00dd1716367174017f1f76de132c78031e793b9f6018c441f39`. Release asset checksums and image/report identity were verified after publication.

| Observation | Load | Soak |
|---|---:|---:|
| Duration | 30.08 s | 1800.12 s |
| Successful calls | 2,704 / 2,704 | 9,000 / 9,000 |
| p95 / p99 | 97.42 / 127.70 ms | 113.16 / 152.02 ms |

The hosted runner exposed 4 CPUs and approximately 15.61 GiB Docker memory. The 5 ms deterministic fixture used one physical host and a verified-TLS PostgreSQL 18 authority; site names are synthetic. West-gateway RSS grew from 110,560 to 113,564 KiB (peak growth 2.93 MiB), below the predeclared 64 MiB gate. These measurements are bounded regression evidence, not production sizing or a leak-absence proof.

[Exact-image lab](evidence/customer-2.1.0.json) · [Installed applications](evidence/customer-installed-clients-2.1.0.json) · [Official native clients](evidence/customer-official-clients-2.1.0.json) · [Published-image SQLite upgrade/recovery](evidence/upgrade-2.1.0.json) · [Delivery identity and job results](evidence/stable-2.1.0-delivery.json). All four upgrade/recovery assertions passed, including unchanged binding and consumed-quota preservation. Real PostgreSQL restore/integration cases passed separately in the release test job; the published-image rollback exercise specifically used default SQLite storage. The runtime advisory report covered 35 resolved Python packages with no known vulnerabilities or skipped packages.

### Final registry delivery

Both the bundled **0.1.0** and standalone **0.1.1** SDK files match their original tested release artifacts on PyPI and npm. The default versions on both registries are **0.1.1**. The [bundled verification run](https://github.com/eturkok/pliqra/actions/runs/42408333982) and [patch verification run](https://github.com/eturkok/pliqra/actions/runs/42408692802) passed both canonical clean-install jobs; npm provenance metadata is present. [Public hashes, runtime identity and registry results](evidence/stable-sdk-registry.json).

Initial post-publication installs exposed a real registry visibility gap: downloadable files and version metadata can be available before installers see the new version. The pipeline now verifies hashed PyPI installer links and bounds installer retries to version-not-found failures, with a 300-second limit. Authentication, TLS and integrity failures stop immediately. Four index cases and ten installer cases protect this behavior; failed historical runs are retained and superseded by the successful verification runs above. No published artifact or tag was overwritten.
