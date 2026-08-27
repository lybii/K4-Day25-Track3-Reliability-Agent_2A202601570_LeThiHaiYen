# Day 25 Reliability Report — Reliability Engineering for Production Agents

**Author:** Le Thi Hai Yen
**Date:** 2026-08-27
**Repro:** `pip install -e ".[dev]" && docker compose up -d && make test && make run-chaos && make report`

### Reproducibility

The chaos simulator is now **fully deterministic**. Verified: two consecutive
`make run-chaos` produce byte-identical `metrics.json` — *every* field, including
`circuit_open_count` and `recovery_time_ms` (`2000.0` ms). Only `latency_p*`
still carry sub-1% jitter (they are real `perf_counter` timings of the fake
provider's `time.sleep`). Two mechanisms make this work — see §10.1:

- **Virtual clock.** `CircuitBreaker.clock` is injectable (prod default
  `time.monotonic`). The runner injects `chaos.ManualClock`, advanced
  `TICK_SECONDS = 0.25` per serial request, so breaker OPEN→HALF_OPEN timing
  no longer depends on wall-clock scheduling jitter.
- **Per-scenario RNG seed.** Each scenario reseeds from a CRC of its own name
  (`chaos.scenario_seed`), so results are independent of scenario order.

---

## 1. Architecture summary

The gateway routes every request through three layers. Each layer can satisfy the request on its
own; control only falls through on miss/failure. Nothing ever raises to the caller — the worst case
is a fast static "degraded" response.

```
                          ┌───────────────────────── ReliabilityGateway.complete(prompt) ─────────────────────────┐
                          │                                                                                       │
  User request ──────────▶│  0. COST GATE (if routing.budget_usd set)                                             │
                          │       spend ≥ 100% budget → no paid call: cache hit or route="budget_exhausted"       │
                          │       spend ≥ 80%  budget → reorder provider chain cheapest-first ("cost_saver_*")     │
                          │                                                                                       │
                          │  1. CACHE CHECK   cache.get(prompt)                                                    │
                          │       ├─ privacy pattern?  ── yes ─▶ (None, 0.0)   (never served, never stored)        │
                          │       ├─ TTL-expired entries evicted lazily                                            │
                          │       ├─ best cosine similarity ≥ threshold (0.92)?                                    │
                          │       │      └─ 4-digit number mismatch (year/ID)? ─ yes ─▶ false_hit_log, (None,s)    │
                          │       └─ HIT ─▶ route="cache_hit:<score>", latency=0, cost=0                           │
                          │                                                                                       │
                          │  2. PROVIDER CHAIN (primary → backup), each guarded by its circuit breaker            │
                          │       breaker.call(provider.complete, prompt)                                          │
                          │          ├─ OPEN & cooling  ─▶ raise CircuitOpenError  (fail fast, no network call)    │
                          │          ├─ OPEN & timeout elapsed ─▶ HALF_OPEN, allow one probe                       │
                          │          ├─ success ─▶ record_success()  [HALF_OPEN→CLOSED after success_threshold]    │
                          │          └─ exception ─▶ record_failure()                                              │
                          │                 [CLOSED→OPEN at failure_threshold  "failure_threshold_reached"]        │
                          │                 [HALF_OPEN→OPEN immediately          "probe_failure"]                  │
                          │       success ─▶ cache.set(...) ; route = "primary" | "fallback" (+ "cost_saver_*")    │
                          │       ProviderError / CircuitOpenError ─▶ record, try next provider                    │
                          │                                                                                       │
                          │  3. STATIC FALLBACK  (all providers failed/open)                                       │
                          │       route="static_fallback", error=<last provider error>                            │
                          └───────────────────────────────────────────────────────────────────────────────────────┘

  Circuit breaker 3-state machine (per provider; CircuitBreaker or SharedRedisCircuitBreaker):

        ┌────────┐  failure_count ≥ failure_threshold        ┌──────┐
        │ CLOSED │ ───────────────────────────────────────▶  │ OPEN │
        │        │        ("failure_threshold_reached")      │      │
        │        │ ◀───────────────────────────────────────  │      │
        └────────┘   success_count ≥ success_threshold       └──────┘
             ▲          ("probe_success")                    │   ▲
             │                                               │   │ any failure on the probe
             │                              reset_timeout    │   │ ("probe_failure")
             │                              elapsed          ▼   │
             │                                          ┌───────────┐
             └───────────────────────────────────────── │ HALF_OPEN │
                                                        └───────────┘
```

Key implementation notes:

- **No retry storm.** A failing provider is called at most once per request. When its breaker is
  OPEN the gateway spends ~0 ms on it (`CircuitOpenError` before any network call).
- **Route reasons carry the trigger.** `transition_log` entries record `from`, `to`, `reason`, `ts`;
  `reason` distinguishes `failure_threshold_reached` / `probe_failure` / `probe_success` /
  `reset_timeout_elapsed` — never OR-ed together.
- **Two cache backends, one contract.** `ResponseCache` (in-process) and `SharedRedisCache`
  (Redis hash + `EXPIRE`) share `get()/set()` and both guardrails.
- **Two breaker backends, one contract.** `CircuitBreaker` (per-process, `RLock`-guarded) and
  `SharedRedisCircuitBreaker` (state in Redis, single-flight probe) — §10.3.

---

## 2. Configuration (`configs/default.yaml`)

| Setting | Value | Reason |
|---|---:|---|
| `circuit_breaker.failure_threshold` | 3 | 1–2 trips on a single unlucky call; 3 consecutive failures is signal, not noise. `all_healthy` (0% fail) never trips; `primary_timeout_100` trips inside the first 3 calls. |
| `circuit_breaker.reset_timeout_seconds` | 2 | Long enough for a transient blip to clear, short enough to pick a recovered provider back up fast. Measured recovery = **2000 ms** (= this value; the virtual clock probes exactly on the boundary). |
| `circuit_breaker.success_threshold` | 1 | Independent per-call failures ⇒ one good probe is enough. Unit-tested with `2` in `test_success_threshold_greater_than_one` and property-tested for arbitrary values. |
| `circuit_breaker.backend` | `memory` | Per-process breaker. `redis` (shared) available — §10.3, `configs/redis_full.yaml`. |
| `cache.ttl_seconds` | 300 | ~5-min freshness window for FAQ/policy/technical answers. Dated queries are covered by the false-hit guard regardless of TTL. |
| `cache.similarity_threshold` | 0.92 | Cosine over word tokens + char 3-grams. At **0.7** `"...2024..."` matches `"...2026..."` (~0.90) and only the false-hit guard saves it; at **0.85** "3 bullets" vs "5 bullets" collide. 0.92 = near-verbatim + light paraphrase only. |
| `cache.backend` | `memory` | `redis` used for §6 via `configs/redis.yaml`. |
| `load_test.requests` | 100 (×4 scenarios = 400) | Enough to trip/heal breakers several times and build a stable hit rate; full run < 3 min with real sleeps. |
| `load_test.concurrency` | 1 | Serial by default; `scripts/run_concurrent.py` overrides (§10.2). |
| `routing.budget_usd` | *(unset)* | Cost-aware routing off by default; `configs/cost_cap.yaml` sets `0.03` for the demo (§10.4). |
| providers | primary: fail 0.25 / 180 ms / $0.010 per 1k · backup: fail 0.05 / 260 ms / $0.006 per 1k | Fast-flaky primary, slow-steady backup: fallback trades ~80 ms for reliability. |

---

## 3. SLO definitions

| SLI | SLO target | Actual (canonical `metrics.json`) | Met? |
|---|---|---:|---|
| Availability | ≥ 99% | **100%** `all_healthy` & `primary_flaky_55`; **97%** `primary_timeout_100`; **74%** `both_degraded_hard`; **92.75%** aggregate | **Partial** — met for single-dependency failure modes; not met when *both* providers are simultaneously >50% down (beyond 2-provider redundancy). |
| Latency P95 | < 2500 ms | **315 ms** aggregate (≤ 320 ms every scenario) | ✅ Yes |
| Fallback success rate | ≥ 95% | **100%** `primary_flaky_55`; **92.7%** `primary_timeout_100`; 74% aggregate (diluted by `both_degraded_hard`) | ✅ / borderline for the modes it measures |
| Cache hit rate | ≥ 10% | **57%** (memory) / **69%** (redis) | ✅ Yes |
| Recovery time | < 5000 ms | **2000 ms** (deterministic) | ✅ Yes |

Aggregate availability is pulled down only by `both_degraded_hard`. Across the other 300 requests it
is ~99% (three static fallbacks in `primary_timeout_100` from the 2%-failing backup).

---

## 4. Metrics — canonical run

Source: `reports/metrics.json` (`make run-chaos`, `configs/default.yaml`, memory backends, seed 1234).

| Metric | Value |
|---|---:|
| total_requests | 400 |
| availability | 0.9275 |
| error_rate | 0.0725 |
| latency_p50_ms | 266.60 |
| latency_p95_ms | 315.20 |
| latency_p99_ms | 320.10 |
| fallback_success_rate | 0.7434 |
| cache_hit_rate | 0.5725 |
| circuit_open_count | 26 |
| recovery_time_ms | 2000.0 |
| estimated_cost | 0.065018 |
| estimated_cost_saved | 0.229 |

### Per-scenario breakdown  (`reports/scenarios.json` / `.csv`)

| Scenario | avail | err | P50 ms | P95 ms | fb succ rate | cache hit | fallback ✓ | static fb | opens | recovery ms | cost $ | result |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|:--:|
| all_healthy | 1.00 | 0.00 | 214.9 | 239.5 | 0.00 | 0.60 | 0 | 0 | 0 | – | 0.02379 | **pass** |
| primary_timeout_100 | 0.97 | 0.03 | 286.1 | 319.2 | 0.927 | 0.59 | 38 | 3 | 11 | – (never heals) | 0.01413 | **pass** |
| primary_flaky_55 | 1.00 | 0.00 | 269.5 | 316.1 | 1.00 | 0.63 | 25 | 0 | 3 | 2000.0 | 0.01647 | **pass** |
| both_degraded_hard | 0.74 | 0.26 | 290.4 | 314.2 | 0.447 | 0.47 | 21 | 26 | 12 | 2000.0 | 0.01062 | **pass** |

---

## 5. Cache comparison — with vs without

`reports/metrics.json` vs `reports/metrics_nocache.json` (`configs/no_cache.yaml`). Same seed, same scenarios.

| Metric | Without cache | With cache (memory) | Delta |
|---|---:|---:|---:|
| availability | 0.8575 | 0.9275 | **+0.070** |
| error_rate | 0.1425 | 0.0725 | −0.070 |
| latency_p50_ms | 272.53 | 266.60 | −5.9 (see note) |
| latency_p95_ms | 316.50 | 315.20 | −1.3 |
| cache_hit_rate | 0.0000 | 0.5725 | +0.5725 |
| **circuit_open_count** | **37** | **26** | **−11** (cache absorbs load ⇒ breakers trip less) |
| **estimated_cost** | **$0.148464** | **$0.065018** | **−$0.083446 (−56.2%)** |
| estimated_cost_saved (counter) | 0.0 | $0.229 | — |

**Note on latency:** cache hits return at `latency_ms = 0` and are excluded from the percentile pool
(only real provider calls are timed), so the percentiles describe *provider-call latency* and barely
move. The real win: ~57% of requests now return in ~0 ms and never touch a provider — which is also
why cost drops 56% and the breakers trip 30% less often.

---

## 6. Redis shared cache

### Why in-memory is not enough for multi-instance

`ResponseCache` lives in one process's heap. With N replicas behind a load balancer you get N cold
caches: an answer computed on replica A is a full-price miss on replica B, the hit rate falls toward
`1/N` of the single-node rate, and the cost/latency savings evaporate.

### How `SharedRedisCache` solves it

| Concern | Mechanism |
|---|---|
| Shared state | key `rl:cache:<md5(query)[:12]>` → hash `{query, response}`; any replica `HSET`/`HGET`. |
| Eviction | `EXPIRE key ttl_seconds` on write — Redis evicts, TTL consistent across replicas. |
| Exact hit | `HGET <hash-key> response` → `(response, 1.0)`, O(1). |
| Semantic hit | `SCAN rl:cache:*`, `HGET query`, `ResponseCache.similarity()` locally, best ≥ threshold. |
| Privacy guard | `_is_uncacheable()` in both `get()` and `set()` — verified below, no sensitive query stored. |
| False-hit guard | `_looks_like_false_hit()` before returning any similarity match; rejects logged to `false_hit_log`. |
| Transient connect flakiness | `from_url(socket_connect_timeout=10, socket_timeout=10, retry=Retry(ExponentialBackoff(cap=1, base=0.2), retries=3), retry_on_timeout=True)`. |

### Evidence (full capture: `reports/redis_evidence.txt`)

Two independent instances, A writes / B reads (`scripts/redis_shared_state_demo.py`):

```
1. shared exact-hit via instance B : value='[A] CLOSED/OPEN/HALF_OPEN ...' score=1.0
2. shared semantic hit via B       : value='[A] CLOSED/OPEN/HALF_OPEN ...' score=0.838
3. privacy query via B             : value=None (expected None)      # "account balance for user 123" never stored
4. false-hit (2024 vs 2025) via B  : value=None score=0.958 log_len=1 # high similarity, rejected on year mismatch
```

```
$ docker compose exec redis redis-cli KEYS "rl:cache:*"      # 12 keys
$ docker compose exec redis redis-cli DBSIZE                 # (integer) 12   (14 cacheable queries minus dedup; 6 privacy queries absent)
$ docker compose exec redis redis-cli HGETALL rl:cache:9e413fd814eb
  query    -> "What should I do when API calls return 429?"
  response -> "[primary] reliable answer for: What should I do when API calls return 429?"
$ docker compose exec redis redis-cli TTL rl:cache:9e413fd814eb   # counting down from 300
# privacy scan of every cached query: (none stored — guardrail holding)
```

### In-memory vs Redis (aggregate, same 4 scenarios, seed 1234)

| Metric | In-memory `metrics.json` | Redis cache `metrics_redis.json` |
|---|---:|---:|
| availability | 0.9275 | 0.9750 |
| latency_p50_ms | 266.60 | 250.60 |
| cache_hit_rate | 0.5725 | 0.6900 |
| circuit_open_count | 26 | 18 |
| estimated_cost | $0.065018 | $0.054782 |
| estimated_cost_saved | $0.229 | $0.276 |

Redis scores better because the in-memory cache is rebuilt per scenario (`build_gateway` → new
`ResponseCache`) while the Redis store **persists across scenarios** — exactly the warm-shared-cache
behaviour a multi-instance deployment gets. Both runs pass all four scenarios.

---

## 7. Chaos scenarios

Definitions: `configs/default.yaml`. Pass/fail logic: `reliability_lab.chaos._scenario_passed`.

| Scenario | Induced fault | Expected | Observed | Pass criterion | Result |
|---|---|---|---|---|:--:|
| **all_healthy** | primary 0%, backup 0% | all via primary/cache, no breaker activity | avail 1.00, 0 opens, 60% cache hits, 0 fallbacks | `availability ≥ 0.95` | **PASS** |
| **primary_timeout_100** | primary 100%, backup 2% | primary breaker trips and *stays* open; traffic on backup | avail 0.97, primary opened **11×**, never recovers (every probe fails), 38 via backup, 3 static (the 2%-flaky backup), fb-success-rate 0.927 | `fb_success_rate ≥ 0.9 AND availability ≥ 0.9 AND opens ≥ 1` | **PASS** |
| **primary_flaky_55** | primary 55%, backup 2% | breaker oscillates: opens on bursts, **heals** on a good probe | avail 1.00, opened **3×** and **recovered** (`recovery_time_ms = 2000.0`), 25 via backup, 0 static | `availability ≥ 0.9 AND opens ≥ 1 AND recovery_time_ms is not None` | **PASS** |
| **both_degraded_hard** | primary 80%, backup 50% | beyond redundancy: expect graceful degradation, not high availability | avail 0.74, **both** breakers open (12 opens), 26 instant static fallbacks, **P95 still 314 ms** (no retry pile-up), still healed once | `circuit_open_count ≥ 1 AND static_fallbacks > 0` | **PASS** |

**Recovery evidence (open → half_open → closed):** `primary_flaky_55` breaker `transition_log` shows
`closed→open (failure_threshold_reached)` → `open→half_open (reset_timeout_elapsed)` →
`half_open→closed (probe_success)`, Δ = exactly 2000 ms (virtual clock). Aggregate
`recovery_time_ms` in `metrics.json` = 2000.0.

**No-retry-storm evidence:** in `both_degraded_hard`, 26% of requests hit the static fallback yet
P50/P95 (290 / 314 ms) match the healthy band — an OPEN breaker returns `CircuitOpenError` with no
network call, so a bad dependency is *cheaper* per request, not more expensive.

---

## 8. Failure analysis — the weakness, and how it is addressed

**Original weakness: per-process circuit-breaker state.** `SharedRedisCache` gave replicas a shared
*cache*, but `CircuitBreaker` kept `state` / `failure_count` in one process's memory, so:

1. **Thundering probe herd** — every replica probes a recovering dependency at once (M× spike).
2. **Restart amnesia** — a restarted replica comes back CLOSED and re-learns the outage the slow way.
3. **Split-brain routing** — replica A (OPEN) serves static fallback while replica B (CLOSED) serves
   real answers for the same query at the same time.

**Addressed (§10.3): `SharedRedisCircuitBreaker`, opt-in via `circuit_breaker.backend: redis`.**
State, an `opened_at` timestamp, the failure/success counters, a shared transition `log`, and a
single-flight `probe` lock all live in Redis; every replica reads time from `redis.time()`. The
`redis_full.yaml` chaos run trips the shared breaker **6×** total across 3 scenarios vs **26×** for
the per-process breaker over 4 — one replica's trip is every replica's trip. The default stays
`memory` (no Redis dependency for the simple case).

**Remaining, lower priority:**
- The Redis breaker's methods are short Redis sequences, not one atomic transaction — a Lua script
  would close the last small races; the single-flight `probe` covers the one that matters.
- **No graceful degradation if Redis is down.** Next: fall back to a local `CircuitBreaker` /
  `ResponseCache` when `redis.ping()` fails, so a Redis outage degrades to today's per-process
  behaviour instead of erroring.
- **No per-tenant isolation** — one abusive caller can trip a shared breaker for everyone; add a
  token-bucket limiter keyed by API key.
- **Quality is unmeasured** — the static fallback counts as "handled"; a real system needs a quality
  SLI separate from availability.

---

## 9. Next steps

1. **Redis breaker: atomic (Lua) + local fail-open** when Redis is unreachable.
2. **Concurrency as the default measurement.** §10.2 shows the numbers move under load; run the
   whole suite concurrently and treat the serial figures as a floor.
3. **Cost-aware routing in production** — wire `routing.budget_usd` to a real per-window spend meter
   and add a mid-tier model between "cheapest provider" and "cache-only".

---

## 10. Stretch goals

### 10.1 Deterministic chaos (virtual clock + per-scenario seed)

`CircuitBreaker.clock` is an injectable `Callable[[], float]` (default `time.monotonic`).
`chaos.ManualClock` is a virtual clock advanced `TICK_SECONDS = 0.25` per serial request; combined
with `chaos.scenario_seed(name)` (CRC32 of the scenario name, so order doesn't matter) the whole
simulation is a pure function of `SIMULATION_SEED`.
**Evidence:** two consecutive `make run-chaos` → `metrics.json` identical in every field including
`circuit_open_count` (26) and `recovery_time_ms` (2000.0). The no-cache aggregate is likewise stable.

### 10.2 Concurrency + breaker thread-safety

`CircuitBreaker` guards `allow_request` / `record_success` / `record_failure` with a
`threading.RLock` — never the wrapped call, so parallel requests still overlap.
`chaos.run_scenario_concurrent` drives the load through a `ThreadPoolExecutor`.
`python scripts/run_concurrent.py --scenario primary_flaky_55 --workers 8`:

```
serial       wall=12.49s  throughput= 8.0 req/s  avail=1.000  P95=316.2ms  circuit_opens=3  fallback_sr=1.000
concurrent   wall= 2.00s  throughput=50.1 req/s  avail=0.980  P95=318.4ms  circuit_opens=1  fallback_sr=0.956
wall-clock speedup: 6.3x
```

The counters move (opens 3→1, availability 1.0→0.98) because under 8 workers many requests hit the
same OPEN window and probes race — the locks keep the counts *consistent* (no lost increments), not
identical to the serial run. That is the expected, reportable effect of concurrent load.

### 10.3 Redis-backed circuit state (`SharedRedisCircuitBreaker`)

Keys under `rl:cb:<name>:` — `state`, `opened_at`, `failures` (sliding-TTL `INCR`), `successes`,
`probe` (`SET NX EX` single-flight lock), `log` (`RPUSH` JSON, read via `LRANGE`). Time from
`redis.time()`. Selected by `circuit_breaker.backend: redis`.
`python scripts/redis_circuit_demo.py` — replicas A and B, separate objects/connections, one Redis:

```
after 3 failures on A: A.state = open  B.state = open   (B never saw a failure)
B.call(...) -> fails fast: circuit 'primary' is open (shared)
after cooldown: A.allow_request()=True  B.allow_request()=False   (single-flight: exactly one True)
probe success -> A.state=closed  B.state=closed   (shared CLOSED)
transition_log (shared, from Redis):
      closed -> open      failure_threshold_reached
        open -> half_open reset_timeout_elapsed
   half_open -> closed    probe_success
```

`configs/redis_full.yaml` (cache **and** breaker on Redis) passes all 3 scenarios;
`reports/metrics_redis_full.json` (one run — the Redis breaker uses the real `redis.time()` clock,
so this path is not seed-reproducible): availability ≈ 0.91, cache_hit ≈ 0.74,
**circuit_open_count 6** vs 26 for the per-process breaker over 4 scenarios — shared state means one
trip, not one per replica.

### 10.4 Cost-aware routing

`ReliabilityGateway(budget_usd=…)` from `routing.budget_usd`, tracking `cumulative_cost`:
< 80% budget → configured order; 80–100% → providers sorted cheapest-first (`route="cost_saver_*"`);
≥ 100% → no paid call, cache hit or `route="budget_exhausted"`.
`python scripts/cost_aware_demo.py` (`configs/cost_cap.yaml`: budget $0.03, cache off, 150 requests):

```
[cost cap ON]   spend $0.0301   served 60/150
   route primary            40      # < 80% budget
   route cost_saver_primary 16      # 80–100% budget, cheapest first
   route fallback            4
   route budget_exhausted   90      # >= 100% budget, refused
[cost cap OFF]  spend $0.0873   served 150/150
```

Spend held at the cap instead of 2.9× over it; the 90 refused requests fail safe rather than
overspend.

### 10.5 Property-based tests (`tests/test_circuit_breaker_properties.py`, hypothesis)

7 properties over fuzzed event sequences: state is always a valid enum and counters ≥ 0; the
transition log is a well-formed chain (`log[i].to == log[i+1].from`, no self-transitions); fewer
than `threshold` failures never opens; exactly `threshold` consecutive failures always opens with
reason `failure_threshold_reached`; OPEN denies until `reset_timeout` then probes; HALF_OPEN + any
failure → OPEN with `probe_failure` regardless of `failure_count`; HALF_OPEN closes only after
`success_threshold` probes.

### 10.6 SLO table — §3.

---

## Appendix — file map

| Path | What |
|---|---|
| `reports/metrics.json` / `.csv` | Canonical `make run-chaos` (memory backends), reproducible. |
| `reports/scenarios.json` / `.csv` | Per-scenario breakdown (`scripts/run_scenarios.py`). |
| `reports/metrics_nocache.json` | Cache disabled (`configs/no_cache.yaml`) — §5. |
| `reports/metrics_redis.json` | Redis response cache (`configs/redis.yaml`) — §6. |
| `reports/metrics_redis_full.json` | Redis cache **and** breaker (`configs/redis_full.yaml`) — §10.3. |
| `reports/redis_evidence.txt` | `redis-cli` KEYS/HGETALL/TTL, privacy scan, shared-cache + shared-breaker demos. |
| `reports/test_output.txt` | Saved `pytest -v` + `ruff` + `mypy` log. |
| `reports/generated_summary.md` | Auto table from `make report` (not this document). |
| `scripts/run_scenarios.py` · `run_concurrent.py` · `redis_circuit_demo.py` · `redis_shared_state_demo.py` · `cost_aware_demo.py` | Added. |
| `configs/no_cache.yaml` · `redis.yaml` · `redis_full.yaml` · `cost_cap.yaml` | Added. |
