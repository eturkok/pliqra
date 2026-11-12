> Current release: **[2.4.1](releases/v2.4.1.md)** adds persistent Light / Dark / System console appearance and retains schema **7**, inference contract **5**, Protocol **1.0** and SDK **1.0.1**. The exact-image release pipeline checks native architectures, theme assets, a 30-minute synthetic soak and upgrade/backup recovery from 2.4.0. Public reports are attached to the [release](https://github.com/eturkok/pliqra/releases/tag/v2.4.1). [2.4.0 delivery](evidence/patch-2.4.0.json) and the earlier records below remain historical.

# Pliqra 2.1 — validation and scope

The published distribution is **[2.1.0](releases/v2.1.0.md)**. [Distribution](DISTRIBUTION.md) documents image verification and the tag-triggered publication gate; the functional acceptance evidence below describes the 2.1 baseline. The [stable delivery record](STABLE_2_1.md#verified-stable-delivery--7-november-2026) contains the final exact-image measurements and release-job results.

The RC3 additions were validated on 7 November 2026; [synthetic acceptance](ACCEPTANCE.md) records streaming, SDK, fleet, load and recovery evidence. The multi-DC baseline below was validated on 6 November 2026. The [2.0 validation record](https://github.com/eturkok/pliqra/blob/62e4ec0d02a9a5d85748bf0072ab6d532cc85abd/docs/RELEASE_2_0.md) and [1.2 record](https://github.com/eturkok/pliqra/blob/62e4ec0d02a9a5d85748bf0072ab6d532cc85abd/docs/RELEASE_1_2.md) remain available as history. The deployment contract, including infrastructure responsibilities, is in [MULTI_DC.md](MULTI_DC.md).

## Stable release gates

[Stable readiness](STABLE_2_1.md) and [support/compatibility](SUPPORT.md) define 2.1.0. The release workflow requires exact-image synthetic acceptance, a predeclared 30-minute soak, RC4 upgrade and encrypted-backup recovery, SDK compatibility and an unsuppressed Python dependency advisory scan before publication. The release carries the measured reports; older numbers below remain historical. SDK 0.1.0 used stable registry versions while that release's wire protocol was draft 0.1.

## RC4 console validation

The console uses a dedicated scroll area between the sidebar header and profile, an accessible tablet/phone menu, labelled mobile table cards, adaptive filters, and bounded dialog/drawer bodies with reachable actions. The in-app browser exercised 102 page/viewport combinations, 12 narrow-screen sub-tabs, 23 form/viewport combinations, three login sizes and three request-drawer sizes. No page-wide horizontal overflow or clipped form inputs remained in those checks. Keyboard containment/return, filter application, visible form errors and login deep links were also checked. [Detailed UI evidence](RESPONSIVE_UI.md).

SDK publisher retries now verify immutable public artifacts and skip redundant uploads. Thirteen regression cases cover matching/different bytes, changed local files, missing versions, denied/unavailable registries and unexpected destinations. The publisher uses a separate checkout for the selected original SDK release.

## RC3 additions

The local Python 3.14 baseline passed **205 tests** with a real PostgreSQL 18 service configured. A final native-usage correction added one regression case. At the published tag, both Python 3.13/3.14 jobs pass **173 tests** with 33 PostgreSQL cases explicitly skipped; the dedicated real-PostgreSQL job passes **43 cases** (including those 33 integration cases and ten shared SQLite fleet cases). Both independently installed SDK applications and all six official-native-client scenarios pass against synthetic providers with verified TLS. The console was exercised for instance visibility, two-target resource mapping and immutable proposal creation; the author sees no self-approval control. No real customer feedback is claimed. [Synthetic acceptance](ACCEPTANCE.md) · [Fleet](FLEET.md) · [SDK installation](SDK.md)

## Delivered behavior

- Active gateway sites share one writable PostgreSQL authority for request/token quotas, concurrency, policies, identities and usage. Database time governs admission windows and node leases.
- Typed service placement supports local preference, local-only routing and allowed model sites/regions. Constraints apply to fallback, canary and cached model selection, participate in fingerprints and can be negotiated as `pliqra.placement/1`.
- Health observations and circuit breakers are isolated by observing site. A failed path in one DC does not poison another DC's reachability.
- Versioned, audited active/draining/standby controls are available in the console and API. Admitted calls finish during drain; new calls and selection of models at that site stop.
- Separate liveness/readiness probes and opt-in supervised restart let fenced gateways rejoin with a fresh process/node identity. Started inference is never automatically replayed.
- Ingress/model-site filters, CSV fields, detailed events and per-attempt model locations make routing visible to administrators. TypeScript and Go binding types expose placement metadata.

The default stays one platform-independent Docker image with SQLite. Optional [multi-DC Compose](../compose.multidc.yaml) runs separately in each DC against your existing verified-TLS PostgreSQL HA service. It does not provision independent writers or a global traffic manager.

## Automated evidence

The 6 November baseline had **159 passing Python tests** on host Python 3.14 with real PostgreSQL, including **23 PostgreSQL integration cases** (16 multi-DC and seven existing storage/scale cases). Without the test database URL, those integration cases skip explicitly. The Linux/Python 3.13 Docker suite exercises the same tests. JavaScript and Go client checks remain part of CI.

The new cases cover concurrent global request admission; pending token reservations in gateway/key/user/project/team/service scopes; global concurrency; local model selection; drain while an admitted call runs; per-site health/circuits; strict placement and canary boundaries; database-clock admission; multi-host writable selection; manual endpoint recovery without discovery; and isolated storage-connection loss. Unit cases additionally reject SQLite/unverified remote databases in multi-DC mode and check placement/traffic-mode contract behavior.

These tests caught and corrected PostgreSQL token-quota edge cases: numeric aggregate values must be normalized before JSON error serialization, and nullable quota scopes require portable null-safe equality. Candidate and contract-inventory reads were batched to avoid per-model WAN round trips.

No assertion is suppressed. Existing Authlib/httpx and SAML deprecation warnings remain.

## Real container acceptance

Two actual gateway containers and two synthetic HTTP model endpoints used a real PostgreSQL 18 instance on one local host. Shared sessions, local selection from both sites, draining one site while the other served, and termination of one gateway's database connection passed. The fenced gateway terminated gracefully and its configured container policy restarted it with a fresh node identity; authenticated access and inference then resumed. [Machine-readable record](evidence/multidc-container.json).

The console was checked against this separate synthetic workspace: both active data centers and live replicas, traffic-mode controls, model-location selection and model-placement forms. Ingress/model-site request filters and details use the same API. The English interface and charcoal/orange styling remain. No existing user workspace was started or seeded.

## Acceptance boundaries

- The container exercise simulates two sites on one host. Geographically separate DCs, WAN failure/latency, real model capacity and PostgreSQL primary promotion require deployment acceptance.
- Exact quota continuity depends on **one writer preserving acknowledged state**. The database platform owns replication, promotion, former-primary fencing and durability. Asynchronous replication may lose recent quota/policy records during promotion. Multi-host connection selection is not leader election.
- Disconnected gateways stop admitting calls. There are no offline quota allowances, independent writable DC copies or inference replay. Unknown usage remains reserved until reconciled or its documented window expires.
- No production RTO/RPO, capacity or availability SLO is claimed. Shared database latency and serialized workspace writes affect throughput.
- Model location labels are administrator declarations. Routing constraints do not certify physical/legal residency of databases, guards, judges, analytics, identities or backups.
- Native API subsets, batch/archive limits and real-vendor acceptance boundaries remain. RC3 adds live Responses/Messages streaming; complete-output checks still buffer. The prior Keycloak evidence is from the 2.0 validation; it was not presented as a new 2.1 vendor certification.

RC3 uses schema **6**, inference contract revision **5** and wire protocol draft **0.1**. Stop old replicas, back up, upgrade every site and renew application bindings. See [the upgrade procedure](MULTI_DC.md#evidence-and-upgrade).
