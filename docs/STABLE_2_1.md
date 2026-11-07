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
