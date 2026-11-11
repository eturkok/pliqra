# Controlled gateway performance comparison

[← Pliqra](../README.md)

These optimizations are delivered in [Gateway **2.2.2**](releases/v2.2.2.md). The controlled comparison below measured source changes against the immutable Gateway **2.2.1** image before publication. No published image, SDK, schema or protocol version is moved. The historical 504.14 ms p95 soak result remains in its [original release record](evidence/patch-2.2.1.json); it is an end-to-end CI observation, not isolated gateway overhead.

## Verified same-runner comparison — 11 November 2026

[Comparison workflow](https://github.com/eturkok/pliqra/actions/runs/43016183872) · [Machine-readable per-round results](evidence/performance-2026-11-11.json)

The same 4-CPU AMD64 runner and PostgreSQL 18 topology tested the immutable 2.2.1 image against source **`60ba2fd`**. Both used the same corrected fixture transport, real budgets/quotas, disabled caching and unchanged baseline OS/Python/dependencies. Each value below is the median of three per-round p95 values (64 measured calls per path per round, with warmup). This is a closed-loop test, not a fixed offered request rate.

| Measurement | Concurrency | Direct HTTP model | TLS relay control | Pliqra 2.2.1 | Optimized source |
|---|---:|---:|---:|---:|---:|
| JSON completion p95 | 1 | 6.27 ms | 6.55 ms | 36.66 ms | **29.69 ms** |
| JSON completion p95 | 8 | 10.42 ms | 10.93 ms | 203.63 ms | **173.66 ms** |
| First streamed content p95 | 1 | 1.89 ms | 5.92 ms | 43.59 ms | **27.52 ms** |
| First streamed content p95 | 8 | 4.92 ms | 15.50 ms | 215.32 ms | **214.13 ms** |

Direct/control columns use the baseline phase; both phases' controls and full p50/p95/p99 round results are preserved in the JSON record. In this experiment JSON p95 decreased about 19% at concurrency 1 and 15% at concurrency 8; single-stream first-content p95 decreased about 37%. **No material concurrent-stream first-content improvement was established.** The synchronous storage path and serialized workspace authority remain constraints. Three rounds are not a confidence interval or a production sizing study.

The unchanged-image transport control measured direct HTTP p95 **47.10 → 6.46 ms** and relay p95 **48.04 → 6.73 ms** when only the fixture enabled TCP_NODELAY. That improvement is deliberately separated from the product comparison. Stream completion is measured at `[DONE]` on every path, so the model fixture's later socket closure is not misattributed to the gateway.

Separately instrumented JSON diagnostics confirmed **97 → 83** explicit SQL calls, **18 → 15** clock reads and **9 → 8** advisory-lock statements. Diagnostic durations are excluded from the timing table. All six benchmark runs completed successfully, removed their owned lab and passed the two-site global-quota check.

Normal [CI](https://github.com/eturkok/pliqra/actions/runs/43016174983) also passed Python 3.13/3.14 (**287 passed**, 37 PostgreSQL-dependent cases explicitly skipped in each general job), **47 real-PostgreSQL cases**, clients, package compatibility, Docker startup and the unsuppressed dependency audit. The site now highlights the historical 30-minute/9,000-request/zero-error soak; its original latency measurement remains linked and unchanged.

## Method

[`benchmark_gateway.py`](../scripts/benchmark_gateway.py) reuses the owned synthetic acceptance lab and compares the same 5 ms model through three paths:

1. Direct HTTP model access.
2. Verified HTTPS through a Caddy relay to that HTTP model, to control for ingress TLS and the extra proxy hop.
3. Verified HTTPS through Pliqra to that same model, with verified-TLS PostgreSQL, a scoped key, project budget, request/token gateway quotas and concurrency controls. Response caching is disabled.

Warm each path at each concurrency and mode, reuse clients, alternate path order across three rounds and report JSON completion and streaming first-content/completion p50/p95/p99. Concurrency is 1 and 8. Compare the **median of the three per-round quantiles**, retaining their ranges; differences between p95 values are not p95 per-request overhead. Profiled runs are separate and excluded from latency comparisons. Diagnostic SQL counts cover explicit driver execute calls, excluding implicit BEGIN/COMMIT.

The candidate comparison image inherits the exact baseline OS/Python/dependencies and replaces only `pliqra/`. Every run creates fresh owned databases/volumes; setup and cleanup are outside measured requests. Global quota checks across both active sites run after the measurements. Reports contain no keys, sessions, prompts or fixture credentials.

## Reproduced bottlenecks and changes

- The original HTTP/1.1 fixture splits headers and small bodies over a connection with Nagle buffering. An isolated local control on the **unchanged** released image reduced direct-model p95 from about 51 ms to 9 ms by setting TCP_NODELAY. This is a test-transport artifact, not a Pliqra optimization. The default historical acceptance behavior is preserved. `--model-nodelay` labels the separately controlled experiment.
- With real quota and budget policies enabled, the local SQL diagnostic counted **97 → 83 explicit SQL calls per JSON request**. DB clock reads fell **18 → 15**, advisory-lock statements **9 → 8**. These counts come from diagnostic passes, not an uninstrumented latency result.
- Combine site mode and maintenance/organization settings in one current read. Remove duplicate synchronous checks within the same admission transaction; authoritative checks before provider IO remain.
- Resolve the effective policy once per preparation/admission phase. No policy, identity or binding cache is introduced across requests or asynchronous checks.
- Include node/site metadata in the initial event insertion and commit selection/connection metadata together.
- Settle pending streaming attempts, health, the event and quota ledger together. Known nonstream usage is still persisted before asynchronous output guards, preserving billing evidence when delivery is blocked.
- Verify chat/streaming capabilities together after a successful stream, preserving the verification timestamp with one write transaction.

Shared PostgreSQL workspace serialization remains intentional. These changes do not remove global consistency, make synchronous storage asynchronous, prove WAN behavior or establish production capacity. A short synthetic comparison does not replace release soak, recovery or installation-specific acceptance.

## Reproduce

Install the development dependencies with `python -m pip install -r requirements-dev.txt`. Run Docker with the acceptance lab ports 18091–18097 free. Use only synthetic endpoints and new lab directories:

```sh
docker pull ghcr.io/eturkok/pliqra@sha256:ac0a44b919c5d469f90d2c1e331601c58780ad70e698e545dfd367914b7efa7f
docker build -f tests/performance/Dockerfile --build-arg REVISION="$(git rev-parse HEAD)" -t pliqra:performance .
python scripts/benchmark_gateway.py --image pliqra:performance --state work/perf-candidate --output work/perf-candidate.json --requests 64 --rounds 3 --model-nodelay
```

Run the identical command against the pinned baseline image in a different new state directory. The [manual comparison workflow](../.github/workflows/performance.yml) runs both variants on one CI runner, tests the transport control separately, then collects separately labelled SQL diagnostics. It uploads only the six explicit public reports. It does not publish a container or declare performance SLOs.

Regression validation: **323 local tests passed** with real PostgreSQL and all three SDK runtimes required. SQLite and two-replica PostgreSQL fault injection check that failed settlement retains pending event/quota state and pending streaming attempts; known JSON provider usage remains recorded. The normal CI matrix remains required.
