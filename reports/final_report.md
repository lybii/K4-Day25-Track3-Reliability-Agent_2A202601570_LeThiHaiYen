# Day 25 Reliability Report — Reliability Engineering for Production Agents

**Author:** Le Thi Hai Yen
**Date:** 2026-08-27
**Repro:** `pip install -e ".[dev]" && docker compose up -d && make test && make run-chaos && make report`

All simulations seed the RNG once with `reliability_lab.chaos.SIMULATION_SEED = 1234`.

**What is reproducible:** every scenario's **pass/fail verdict**; and the full metrics of the three
single-dependency scenarios (`all_healthy`, `primary_timeout_100`, `primary_flaky_50`) — identical
run to run except for sub-1% wall-clock jitter on `latency_p*` / `recovery_time_ms` (the fake
provider uses real `time.sleep`, and `CircuitBreaker.allow_request()` compares real
`time.monotonic()`).

**What is not:** the exact counters of `both_degraded_hard`, and therefore the *cached* aggregate in
`metrics.json`. That scenario runs in a feedback regime — cache hits reduce provider load → a breaker
gets a chance to probe → it recovers → more provider calls → more cache fills — where small timing
differences amplify. Observed `both_degraded_hard` availability ranged 0.12–0.45 across runs
(cited numbers below are from the committed `reports/*.json`). The **no-cache** aggregate has no such
feedback loop and *is* fully reproducible (availability 0.7725, cost $0.15011 every run).

---

## 1. Architecture summary

The gateway routes every request through three layers. Each layer can satisfy the request on its
own; control only falls through on miss/failure. Nothing ever raises to the caller — the worst case
is a fast static "degraded" response.

```
                          ┌───────────────────────── ReliabilityGateway.complete(prompt) ─────────────────────────┐
                          │                                                                                       │
  User request ──────────▶│  1. CACHE CHECK                                                                        │
                          │     cache.get(prompt)                                                                  │
                          │        ├─ privacy pattern?  ── yes ─▶ (None, 0.0)  ── skip cache, never store          │
                          │        ├─ TTL-expired entries evicted lazily                                           │
                          │        ├─ best cosine similarity ≥ threshold (0.92)?                                    │
                          │        │      └─ 4-digit number mismatch (year/ID)? ─ yes ─▶ false_hit_log, (None,s)   │
                          │        └─ HIT ─▶ return route="cache_hit:<score>", latency=0, cost=0                    │
                          │                                                                                        │
                          │  2. PROVIDER FALLBACK CHAIN  (ordered: primary → backup)                               │
                          │     for provider in providers:                                                         │
                          │        breaker = breakers[provider.name]                                               │
                          │        breaker.call(provider.complete, prompt)                                          │
                          │           ├─ state OPEN & cooling ─▶ raise CircuitOpenError (fail fast, no network)     │
                          │           ├─ state OPEN & timeout elapsed ─▶ HALF_OPEN, allow 1 probe                   │
                          │           ├─ success ─▶ record_success() [HALF_OPEN→CLOSED after success_threshold]     │
                          │           └─ exception ─▶ record_failure() [CLOSED→OPEN at failure_threshold;           │
                          │                                            HALF_OPEN→OPEN immediately ("probe_failure")]│
                          │        on success  ─▶ cache.set(prompt, text); route = "primary" | "fallback"           │
                          │        on ProviderError / CircuitOpenError ─▶ record error, try next provider           │
                          │                                                                                        │
                          │  3. STATIC FALLBACK  (every provider failed or open)                                    │
                          │     return route="static_fallback", error=<last provider error>,                       │
                          │            text="The service is temporarily degraded. Please try again soon."          │
                          └────────────────────────────────────────────────────────────────────────────────────────┘

  Circuit breaker 3-state machine (per provider):

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
  OPEN the gateway spends ~0 ms on it (`CircuitOpenError` before any network call) and moves straight
  to the backup or the static fallback.
- **Route reasons carry the trigger.** `transition_log` entries record `from`, `to`, `reason`, `ts`.
  `reason` is `failure_threshold_reached` vs `probe_failure` vs `probe_success` vs
  `reset_timeout_elapsed` — different triggers, never OR-ed together.
- **Cache is shared-safe.** `ResponseCache` (in-memory, per-process) and `SharedRedisCache` (Redis
  hash + `EXPIRE`, cross-process) implement the same `get()/set()` contract and the same two
  guardrails (privacy pattern, 4-digit false-hit).

---

## 2. Configuration (`configs/default.yaml`)

| Setting | Value | Reason |
|---|---:|---|
| `circuit_breaker.failure_threshold` | 3 | 1–2 trips on a single unlucky call (primary fail_rate 0.25 → ~6% chance of 2-in-a-row even when healthy); 3 consecutive failures is a real signal, not noise. Verified: `all_healthy` (primary 0%) and `primary_flaky_50` both stay up; `primary_timeout_100` trips within the first 3 calls. |
| `circuit_breaker.reset_timeout_seconds` | 2 | Long enough that a transient blip clears before the probe, short enough that a recovered provider is picked back up quickly. Observed recovery time ≈ 2.23 s ≈ this value + one request cycle. |
| `circuit_breaker.success_threshold` | 1 | Fake provider failures are independent per call, so one good probe is sufficient evidence. A real provider with correlated failures would want 2–3. Unit-tested with `success_threshold=2` in `test_success_threshold_greater_than_one`. |
| `cache.ttl_seconds` | 300 | Matches a plausible "answers stable for ~5 min" window for FAQ/policy/technical queries. The dated queries (`2024` vs `2026`) are protected by the false-hit guard regardless of TTL. |
| `cache.similarity_threshold` | 0.92 | Cosine over word tokens + char 3-grams. Tested lower: at **0.7**, `"Summarize refund policy 2024"` matches `"...2026"` at score ≈ 0.90 and only the false-hit guard saves it; at **0.85** paraphrases of *different* FAQ bullets ("in 3 bullets" vs "in 5 bullets") collide. 0.92 accepts near-verbatim repeats and light paraphrase only. |
| `cache.backend` | `memory` | Default for `make run-chaos`. `redis` backend exercised separately via `configs/redis.yaml` (Section 6). |
| `load_test.requests` | 100 (× 4 scenarios = 400) | Enough to open/close breakers several times and build a stable cache hit rate over the 14 cacheable queries, while keeping a full run < 3 min with real sleeps. |
| providers | primary: fail 0.25 / 180 ms / $0.010 per 1k · backup: fail 0.05 / 260 ms / $0.006 per 1k | Primary = fast, cheap-ish, flakier; backup = slower, cheaper, steadier. Fallback therefore trades ~80 ms latency for reliability. |

---

## 3. SLO definitions

| SLI | SLO target | Actual (canonical `metrics.json`) | Met? |
|---|---|---:|---|
| Availability | ≥ 99% | **100%** in `all_healthy`, `primary_timeout_100`, `primary_flaky_50`; **25%** in `both_degraded_hard`; **86.25%** aggregate | **Partial** — met for every single-dependency failure mode; not met when *both* providers are simultaneously >50% down (beyond 2-provider redundancy). |
| Latency P95 | < 2500 ms | **318.2 ms** aggregate (≤ 319 ms in every scenario) | ✅ Yes |
| Fallback success rate | ≥ 95% | **100%** in `primary_timeout_100` & `primary_flaky_50`; 56% aggregate (diluted by `both_degraded_hard`) | ✅ Yes for the modes it is meant to measure |
| Cache hit rate | ≥ 10% | **57%** (memory), **~73%** (redis) | ✅ Yes |
| Recovery time | < 5000 ms | **≈ 2255 ms** aggregate mean OPEN→CLOSED (2233.74 ms in `primary_flaky_50`) | ✅ Yes |

The aggregate availability line is pulled down entirely by the deliberately catastrophic
`both_degraded_hard` scenario. Across the other 300 requests, availability is 100%.

---

## 4. Metrics — canonical run

Source: `reports/metrics.json` (produced by `make run-chaos`, `configs/default.yaml`, memory cache, seed 1234).

| Metric | Value |
|---|---:|
| total_requests | 400 |
| availability | 0.8625 |
| error_rate | 0.1375 |
| latency_p50_ms | 271.63 |
| latency_p95_ms | 318.23 |
| latency_p99_ms | 320.09 |
| fallback_success_rate | 0.5635 |
| cache_hit_rate | 0.57 |
| circuit_open_count | 11 |
| recovery_time_ms | 2255.28 |
| estimated_cost | 0.05161 |
| estimated_cost_saved | 0.228 |

### Per-scenario breakdown

Source: `reports/scenarios.json` / `reports/scenarios.csv` (`python scripts/run_scenarios.py`).

| Scenario | avail | err | P50 ms | P95 ms | fb success rate | cache hit | fallback ✓ | static fb | circuit opens | recovery ms | cost $ | result |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|:--:|
| all_healthy | 1.00 | 0.00 | 209.3 | 239.3 | 0.00 | 0.68 | 0 | 0 | 0 | – | 0.01820 | **pass** |
| primary_timeout_100 | 1.00 | 0.00 | 289.5 | 318.4 | 1.00 | 0.63 | 37 | 0 | 5 | – (never heals) | 0.01386 | **pass** |
| primary_flaky_50 | 1.00 | 0.00 | 274.5 | 317.3 | 1.00 | 0.66 | 23 | 0 | 2 | 2233.74 | 0.01368 | **pass** |
| both_degraded_hard | 0.25 | 0.75 | 309.3 | 319.1 | 0.06 | 0.20 | 5 | 75 | 3 | – (this run) | 0.001806 | **pass** |

(`both_degraded_hard` counters vary run-to-run — see the reproducibility note at the top. Its
verdict does not.)

---

## 5. Cache comparison — with vs without

`make run-chaos` (cache on, `configs/default.yaml`) vs `python scripts/run_chaos.py --config configs/no_cache.yaml`
(`reports/metrics.json` vs `reports/metrics_nocache.json`). Same seed, same scenarios.

| Metric | Without cache | With cache (memory) | Delta |
|---|---:|---:|---:|
| availability | 0.7725 | 0.8625 | **+0.090** |
| error_rate | 0.2275 | 0.1375 | −0.090 |
| latency_p50_ms | 261.24 | 271.63 | +10.4 (see note) |
| latency_p95_ms | 314.33 | 318.23 | +3.9 (see note) |
| cache_hit_rate | 0.0000 | 0.5700 | +0.5700 |
| **circuit_open_count** | **20** | **11** | **−9** (cache roughly halves provider load → roughly halves the breaker trips) |
| **estimated_cost** | **$0.15011** | **$0.05161** | **−$0.09850 (−65.6%)** |
| estimated_cost_saved (counter) | 0.0 | $0.228 | — |

**Note on latency:** cache hits are returned at `latency_ms = 0` and are *excluded* from the
percentile pool (only real provider calls are timed). So the percentiles above describe
*provider-call latency only* and barely move; the real user-visible win is that ~57% of requests
now return in ~0 ms and never touch a provider. That same effect is why cost drops ~66% and the
breaker trips roughly half as often.

**Cost math:** primary ≈ $0.010 / 1k tokens, backup ≈ $0.006 / 1k tokens, ~50–100 tokens per
call. 400 requests × ~57% served from cache ≈ 228 provider calls avoided ≈ $0.10 saved on a
$0.15 baseline.

---

## 6. Redis shared cache

### Why in-memory is not enough for multi-instance

`ResponseCache` lives in one process's heap. Run N gateway replicas behind a load balancer and you
get N cold caches: a question answered by replica A is a full-price miss on replica B, hit rate
collapses toward `1/N` of the single-node rate, and cost/latency savings evaporate. There is also
no shared view of the privacy/false-hit guardrails — each replica re-learns them.

### How `SharedRedisCache` solves it

| Concern | Mechanism |
|---|---|
| Shared state | One Redis; key = `rl:cache:<md5(query)[:12]>`, value = hash `{query, response}`. Any replica `HSET`s, any replica `HGET`s. |
| Eviction | `EXPIRE key ttl_seconds` on write — Redis evicts, no per-process sweep, TTL is consistent across replicas. |
| Exact hit | `HGET <hash-key> response` → `(response, 1.0)`. O(1), no scan. |
| Semantic hit | `SCAN rl:cache:*`, `HGET query`, `ResponseCache.similarity()` locally, keep best ≥ threshold. |
| Privacy guard | `_is_uncacheable()` checked in both `get()` and `set()` — sensitive queries are never written to Redis. Verified below. |
| False-hit guard | `_looks_like_false_hit()` applied before returning any similarity match; rejected matches go to `false_hit_log`. |
| Transient connect flakiness | `from_url(..., socket_connect_timeout=10, socket_timeout=10, retry=Retry(ExponentialBackoff(cap=1.0, base=0.2), retries=3), retry_on_timeout=True)` — the first connection through Docker Desktop's port-forward can be slow; a bounded retry hides that without hiding a real outage. |

### Evidence of shared state

`python scripts/redis_shared_state_demo.py` — instance **A** writes, a *separate* object/connection
**B** reads (full output in `reports/redis_evidence.txt`):

```
1. shared exact-hit via instance B : value='[A] CLOSED/OPEN/HALF_OPEN ...' score=1.0
2. shared semantic hit via B       : value='[A] CLOSED/OPEN/HALF_OPEN ...' score=0.838
3. privacy query via B             : value=None (expected None)      # "account balance for user 123" never stored
4. false-hit (2024 vs 2025) via B  : value=None score=0.958 log_len=1 # high similarity, rejected on year mismatch
```

### Redis CLI output

After `python scripts/run_chaos.py --config configs/redis.yaml --out reports/metrics_redis.json`
(full capture in `reports/redis_evidence.txt`):

```
$ docker compose exec redis redis-cli KEYS "rl:cache:*"
 rl:cache:9e413fd814eb   rl:cache:3dab98c0e49e   rl:cache:d354658dc020   rl:cache:dacb2b833659
 rl:cache:734852f3cf4a   rl:cache:b2a52f7dc795   rl:cache:da61fb49b4f6   rl:cache:095946136fea
 rl:cache:98332d0d1c9c   rl:cache:844ef0143a5c   rl:cache:0bc3b1acf73d   rl:cache:fff10da1c72c

$ docker compose exec redis redis-cli DBSIZE
(integer) 12                       # the 14 cacheable sample queries minus dedup; the 6 privacy queries are absent

$ docker compose exec redis redis-cli HGETALL rl:cache:9e413fd814eb
1) "query"     2) "What should I do when API calls return 429?"
3) "response"  4) "[primary] reliable answer for: What should I do when API calls return 429?"

$ docker compose exec redis redis-cli TTL rl:cache:9e413fd814eb
(integer) 36                        # counting down from ttl_seconds=300

# privacy check: none of the 6 privacy queries (balance / password / ssn / credit card / account N / user N) are present
(none found - privacy guardrail holding)
```

### In-memory vs Redis run (aggregate, same 4 scenarios, seed 1234)

| Metric | In-memory (`metrics.json`) | Redis (`metrics_redis.json`) |
|---|---:|---:|
| availability | 0.8625 | 0.9575 |
| latency_p50_ms | 271.63 | 232.44 |
| latency_p95_ms | 318.23 | 306.99 |
| cache_hit_rate | 0.5700 | 0.7300 |
| circuit_open_count | 11 | 7 |
| estimated_cost | $0.05161 | $0.041952 |
| estimated_cost_saved | $0.228 | $0.292 |

(The Redis aggregate *is* reproducible — Redis persistence across scenarios dominates the
cache/breaker feedback that makes the memory `both_degraded_hard` wobble.)

Redis scores *better* here for one structural reason: the in-memory cache is rebuilt fresh for every
scenario (`build_gateway` → new `ResponseCache`), while the Redis store **persists across scenarios**.
By the time `primary_flaky_50` runs, Redis is already warm from `all_healthy` + `primary_timeout_100`,
so almost every query is a hit, the primary provider is barely called, and its breaker never trips
(`circuit_open_count == 0` for that scenario → `primary_flaky_50` is marked `fail` in
`metrics_redis.json` by the "breaker must engage" criterion). This is not a regression — it is exactly
the multi-instance behaviour we want (a warm shared cache shields providers) — but it means
**per-scenario chaos isolation requires either the memory backend or a per-scenario key prefix**.
The canonical graded run therefore uses the memory backend; the Redis run is supplementary evidence
for this section.

---

## 7. Chaos scenarios

Definitions in `configs/default.yaml`; pass/fail logic in `reliability_lab.chaos._scenario_passed`.

| Scenario | Induced fault | Expected behaviour | Observed behaviour | Pass criterion | Result |
|---|---|---|---|---|:--:|
| **all_healthy** | primary 0%, backup 0% | Everything via primary/cache, no breaker activity | availability 1.00, 0 circuit opens, 68% cache hits, 0 fallbacks | `availability ≥ 0.95` | **PASS** |
| **primary_timeout_100** | primary 100%, backup 2% | Primary breaker trips fast and *stays* open; all live traffic on backup; no static fallbacks | availability 1.00, primary breaker opened 5×, **never recovers** (every probe fails), 37 requests served by backup, 0 static fallbacks, fallback-success-rate 1.00 | `fallback_success_rate ≥ 0.9 AND availability ≥ 0.9 AND circuit_open_count ≥ 1` | **PASS** |
| **primary_flaky_50** | primary 50%, backup 2% | Breaker oscillates: opens on failure bursts, **heals** on a good probe | availability 1.00, breaker opened 2× and **recovered** (`recovery_time_ms = 2233.74` ≈ reset_timeout 2 s + 1 cycle), 23 requests via backup, 0 static fallbacks | `availability ≥ 0.8 AND circuit_open_count ≥ 1` (heal evidence: `recovery_time_ms` populated) | **PASS** |
| **both_degraded_hard** | primary 80%, backup 50% | Beyond redundancy: do **not** expect high availability; expect graceful degradation — both breakers open, static fallback absorbs the storm, latency stays low (fail fast, no hang) | availability 0.12–0.45 across runs (0.25 in the committed run), **both** breakers open (3–4 opens), 55–88 static fallbacks returned instantly, **P95 still ~319 ms** (no retry pile-up) | `circuit_open_count ≥ 1 AND static_fallbacks > 0` | **PASS** |

**Recovery evidence (open → half_open → closed):** `primary_flaky_50`, from a breaker `transition_log`:
`closed→open (failure_threshold_reached)` … 2 s later … `open→half_open (reset_timeout_elapsed)` →
`half_open→closed (probe_success)`, Δ ≈ 2.23 s. Aggregate `recovery_time_ms` in `metrics.json` = 2255.28 ms.

**No-retry-storm evidence:** in `both_degraded_hard`, 75% of requests hit the static fallback, yet
P50/P95 latency (309 / 319 ms) is in the same band as the healthy run. An OPEN breaker returns
`CircuitOpenError` with no network call, so a total outage is *cheaper* per request, not more expensive.

---

## 8. Failure analysis — one remaining weakness

**Weakness: circuit-breaker state is per-process, so it does not survive a restart or scale out.**
`SharedRedisCache` gives replicas a shared *cache*, but each replica keeps its own
`CircuitBreaker.state` / `failure_count` in memory. Consequences:

1. **Thundering probe herd.** When a dead dependency's `reset_timeout` elapses, *every* replica
   sends a probe at once. With M replicas the recovering provider takes an M× spike exactly when it
   is most fragile.
2. **Restart amnesia.** A replica that restarts (deploy, crash, autoscale) comes back CLOSED and
   will re-discover a still-broken dependency the slow way — 3 more real failed calls per replica.
3. **Split-brain routing.** Replica A (breaker OPEN) serves static fallback while replica B (breaker
   still CLOSED) serves real answers, for the same query, at the same time.

**Fix I would ship before production:** move the breaker counters into Redis, mirroring the cache
design.
- `INCR rl:cb:<provider>:failures` + `EXPIRE` (sliding window) on each failure; read it in
  `record_failure()` to decide OPEN.
- `SET rl:cb:<provider>:state open EX <reset_timeout> NX` — the `NX` makes exactly one replica win
  the probe; the rest keep failing fast until that replica flips the key to `closed`.
- Keep a *local* copy as a cache of the Redis state, refreshed every ~250 ms, and **fail open to the
  local breaker if Redis is unreachable** so a Redis outage degrades to today's behaviour rather than
  breaking routing.

This is the "Redis circuit state" stretch goal and is the single change with the best
reliability-per-line ratio left in the system. It would also make `both_degraded_hard` reproducible,
since the shared state removes the per-replica timing feedback loop.

Secondary weaknesses, lower priority:
- **No per-user / per-tenant isolation.** One abusive caller can trip a shared breaker for everyone.
  Add a token-bucket rate limiter keyed by API key ahead of the provider chain.
- **Quality is unmeasured.** The static fallback counts as "handled" but is useless to the user.
  A real system needs a quality SLI (e.g. answered-vs-degraded ratio) separate from availability.
- **`estimated_cost_saved` is a flat $0.001/hit approximation.** Fine for a relative with/without
  comparison, not for finance. Track real avoided `input+output` tokens per hit.

---

## 9. Next steps

1. **Redis-backed circuit state** with single-flight probes and local fail-open (Section 8) — turns
   the breaker from per-replica to cluster-wide and makes the worst-case scenario deterministic.
2. **Concurrency in the load test.** `run_scenario` is serial; wrap the request loop in a
   `ThreadPoolExecutor` and re-measure — P95 and `circuit_open_count` will both rise under real
   contention and the current numbers are a floor.
3. **Cost-aware routing.** Track cumulative spend per window; at 80% of budget route new misses to
   the cheaper backup first, at 100% go cache-only + static. The hook (`estimated_cost`) is already
   threaded through `GatewayResponse`.

---

## Appendix — file map

| Path | What |
|---|---|
| `reports/metrics.json` / `.csv` | Canonical `make run-chaos` output (memory cache). |
| `reports/scenarios.json` / `.csv` | Per-scenario breakdown (`python scripts/run_scenarios.py`). |
| `reports/metrics_nocache.json` / `.csv` | Cache-disabled run (`configs/no_cache.yaml`) for Section 5. |
| `reports/metrics_redis.json` / `.csv` | Redis-backed run (`configs/redis.yaml`) for Section 6. |
| `reports/redis_evidence.txt` | `redis-cli` KEYS/HGETALL/TTL + privacy check + shared-state demo. |
| `reports/generated_summary.md` | Auto-generated table from `make report` (not this document). |
| `scripts/run_scenarios.py` | Per-scenario runner (added). |
| `scripts/redis_shared_state_demo.py` | Two-instance shared-state proof (added). |
| `configs/no_cache.yaml`, `configs/redis.yaml` | Config variants (added). |
