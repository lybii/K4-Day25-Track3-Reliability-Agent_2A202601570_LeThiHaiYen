# Day 25 Lab Assignments — Reliability Engineering for Production Agents

Build a production-style reliability layer for an LLM agent gateway. The starter repo provides core architecture, interfaces, tests, and TODO zones — you implement all the reliability logic from scratch.

## Learning goals

By the end, you should be able to:

1. Implement a circuit breaker 3-state machine (CLOSED → OPEN → HALF_OPEN → CLOSED).
2. Route requests through a cache → circuit breaker → provider fallback chain.
3. Build semantic cache with n-gram similarity, privacy guardrails, and false-hit detection.
4. Implement a shared Redis cache for multi-instance deployments using Docker.
5. Run chaos scenarios and capture metrics: availability, error rate, P50/P95/P99 latency, fallback success rate, cache hit rate, recovery time, and estimated cost saved.
6. Produce a reproducible report graded from evidence, not opinion.

## Time design

Total lab time: **4–5 hours**.

| Time | Milestone | Deliverable |
|---:|---|---|
| 0–30 min | Setup, run tests, read all TODOs | Test log showing 25 failures + 7 xfail |
| 30–90 min | Circuit breaker: `allow_request()`, `call()`, `record_success()`, `record_failure()` | 11 CB tests passing |
| 90–150 min | Cache: `similarity()`, `get()`, `set()` with n-gram cosine + guardrails | 9 cache tests passing |
| 150–195 min | Gateway: `complete()` — cache → breaker → fallback chain | 4 gateway tests passing |
| 195–240 min | Chaos: `run_scenario()`, `calculate_recovery_time_ms()`, metrics CSV export | `reports/metrics.json` |
| 240–270 min | Redis shared cache: `SharedRedisCache.get()` / `set()` | Redis tests passing |
| 270–300 min | Load test + final report | Final report + metrics JSON/CSV |

## Quickstart

```bash
# Option A: conda
conda activate ai-lab
pip install -e ".[dev]"

# Option B: venv
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Start Redis for shared cache (requires Docker)
make docker-up

# Run tests — 25 will FAIL until you implement TODOs
make test

# Run chaos simulation (requires gateway + circuit breaker + cache implemented)
make run-chaos

# Generate report from metrics
make report
```

## What you need to implement

### Overview: 25 failing tests → 0 failures

Run `make test` to see all 25 failing tests. Each maps to a TODO in source code. Fix TODOs → tests pass.

```
tests/test_circuit_breaker.py  — 11 tests (circuit breaker state machine)
tests/test_cache.py            — 9 tests  (cache similarity, privacy, false-hit)
tests/test_gateway_contract.py — 4 tests  (gateway routing pipeline)
tests/test_todo_requirements.py — 7 xfail (pass when all TODOs done)
tests/test_config.py           — 2 tests  (already passing)
tests/test_metrics.py          — 2 tests  (already passing)
tests/test_redis_cache.py      — 6 tests  (skipped until Redis running)
```

### File-by-file TODO map

| File | Methods to implement | Tests |
|---|---|---|
| `circuit_breaker.py` | `allow_request()`, `call()`, `record_success()`, `record_failure()` | 11 in test_circuit_breaker.py |
| `cache.py` | `ResponseCache.similarity()`, `ResponseCache.get()`, `ResponseCache.set()` | 9 in test_cache.py |
| `cache.py` | `SharedRedisCache.get()`, `SharedRedisCache.set()` | 6 in test_redis_cache.py |
| `gateway.py` | `ReliabilityGateway.complete()` | 4 in test_gateway_contract.py |
| `chaos.py` | `run_scenario()`, `calculate_recovery_time_ms()` | Via run-chaos |
| `metrics.py` | `write_csv()` | 1 in test_todo_requirements.py |

## Repository structure

```
src/reliability_lab/
  circuit_breaker.py   # TODO: implement 4 methods — full state machine from scratch
  cache.py             # TODO: implement similarity (n-gram cosine), get/set with guardrails,
                       #       SharedRedisCache get/set for Redis backend
  gateway.py           # TODO: implement complete() — cache → breaker → fallback pipeline
  chaos.py             # TODO: implement run_scenario(), calculate_recovery_time_ms()
  metrics.py           # TODO: implement write_csv() export
  providers.py         # FakeLLMProvider — simulates latency/failures/cost (NO changes needed)
  config.py            # Pydantic config loader (NO changes needed)

scripts/
  run_chaos.py             # CLI entry point for chaos simulation (also writes .csv)
  generate_report.py       # Auto summary -> reports/generated_summary.md
  run_scenarios.py         # Per-scenario metrics -> reports/scenarios.{json,csv}
  run_concurrent.py        # Stretch: serial vs ThreadPoolExecutor comparison
  redis_circuit_demo.py    # Stretch: shared circuit-breaker state across instances
  redis_shared_state_demo.py  # Two-instance shared-cache proof
  cost_aware_demo.py       # Stretch: budget cap routing behaviour

configs/
  # default.yaml + variants: no_cache.yaml, redis.yaml (cache on Redis),
  #                          redis_full.yaml (cache + breaker on Redis), cost_cap.yaml
  default.yaml         # Provider fail rates, CB thresholds, cache settings, chaos scenarios

tests/                 # Your target — make all tests green
  test_circuit_breaker.py    # 11 tests for 3-state machine
  test_cache.py              # 9 tests for similarity + guardrails
  test_gateway_contract.py   # 4 tests for routing pipeline
  test_todo_requirements.py  # 7 xfail markers — all should unexpectedly PASS when done
  test_redis_cache.py        # 6 Redis tests — skipped if Redis not running
  test_config.py             # 2 tests (already passing)
  test_metrics.py            # 2 tests (already passing)

data/
  sample_queries.jsonl  # 20 queries with risk labels (privacy, technical, faq, dated)

docker-compose.yml     # Local Redis for shared cache
reports/
  report_template.md   # Copy and fill in for final report
```

---

## Step-by-step guide

### Phase 1: Circuit Breaker (30–90 min) — 25 points

Implement 4 methods in `circuit_breaker.py`. Each has detailed TODO comments with exact logic.

**`allow_request()`** — State-based gate:
- CLOSED → always allow
- HALF_OPEN → allow (probe request)
- OPEN → check if `reset_timeout_seconds` elapsed since `opened_at`; if yes, transition to HALF_OPEN and allow; if no, deny

**`call(fn, *args, **kwargs)`** — Wrapper:
- Check `allow_request()` → raise `CircuitOpenError` if denied
- Try `fn(*args, **kwargs)` → `record_success()` on success, `record_failure()` + re-raise on exception

**`record_success()`** — Counter logic:
- Reset `failure_count` to 0, increment `success_count`
- If HALF_OPEN and `success_count >= success_threshold` → transition to CLOSED

**`record_failure()`** — **Tricky part** (most common student mistake):
- Increment `failure_count`, reset `success_count` to 0
- If HALF_OPEN → immediately re-open with reason `"probe_failure"` (separate from threshold)
- Elif `failure_count >= failure_threshold` → open with reason `"failure_threshold_reached"`
- These MUST be `if/elif`, NOT combined with `or` — different reasons for different triggers

**Verify:** `pytest tests/test_circuit_breaker.py -v` → 11/11 passing

---

### Phase 2: Cache (90–150 min) — 15 points

Implement 3 methods in `cache.py` `ResponseCache` class.

**`similarity(a, b)`** — Cosine similarity over character n-grams + word tokens:
1. If `a == b`, return 1.0
2. Tokenize: split into words + character 3-grams (e.g., `"hello"` → `["hello", "hel", "ell", "llo"]`)
3. Build `Counter` vectors from tokens
4. Compute cosine: `dot(a,b) / (|a| × |b|)` using `math.sqrt`

You need to add imports: `from collections import Counter` and `import math`.

**`get(query)`** — Lookup with guardrails:
1. Return `(None, 0.0)` if `_is_uncacheable(query)` — privacy check
2. Evict expired entries (check `time.time() - created_at > ttl_seconds`)
3. Find best match via `self.similarity(query, entry.key)`
4. If score >= threshold: check `_looks_like_false_hit()` → log and reject if true
5. Return `(value, score)` or `(None, score)`

You need to add `self.false_hit_log: list[dict[str, object]] = []` in `__init__`.

**`set(query, value, metadata)`** — Store with privacy guard:
1. Return immediately if `_is_uncacheable(query)`
2. Append `CacheEntry` to `self._entries`

**Verify:** `pytest tests/test_cache.py -v` → 9/9 passing

---

### Phase 3: Gateway (150–195 min) — 25 points

Implement `complete(prompt)` in `gateway.py`. Detailed pipeline:

1. **Cache check** → if cache exists, try `cache.get(prompt)`. On hit, return `GatewayResponse` with `route=f"cache_hit:{score:.2f}"`, `cache_hit=True`
2. **Provider chain** → iterate `self.providers`:
   - Get breaker: `self.breakers[provider.name]`
   - Call via breaker: `breaker.call(provider.complete, prompt)`
   - On success: cache result, determine route (`"primary"` for first provider, `"fallback"` for rest), return response
   - On `ProviderError` or `CircuitOpenError`: save error string, continue
3. **Static fallback** → all providers failed: return degraded message with `route="static_fallback"`, `error=last_error`

**Verify:** `pytest tests/test_gateway_contract.py -v` → 4/4 passing

---

### Phase 4: Chaos + Metrics (195–240 min) — 15 points

**`run_scenario()`** in `chaos.py` — Run N requests through gateway, collect metrics:
- Build gateway with `build_gateway(config, scenario.provider_overrides)`
- Loop `config.load_test.requests` times, pick random query, call `gateway.complete()`
- Track: total, success/fail, cache hits, fallback counts, latencies, cost
- Count circuit open transitions from breaker logs

**`calculate_recovery_time_ms()`** — Walk transition logs:
- Find pairs: `to="open"` → `to="closed"`, compute delta in ms
- Return average, or None if no recovery

**`write_csv()`** in `metrics.py` — Export:
- Flatten `to_report_dict()`, expand scenarios as `scenario_{name}` columns
- Write single-row CSV with `csv.DictWriter`

**Verify:** `make run-chaos` produces `reports/metrics.json`

---

### Phase 5: Redis Shared Cache (240–270 min) — 15 points

Start Redis: `make docker-up`

Implement `SharedRedisCache.get()` and `set()` in `cache.py`:

**`set(query, value)`:**
1. Skip if `_is_uncacheable(query)`
2. Build key: `f"{self.prefix}{self._query_hash(query)}"`
3. `self._redis.hset(key, mapping={"query": query, "response": value})`
4. `self._redis.expire(key, self.ttl_seconds)`

**`get(query)`:**
1. Skip if uncacheable
2. Try exact match: `hget(exact_key, "response")` → return `(response, 1.0)`
3. Similarity scan: `scan_iter(prefix*)`, `hget` each `"query"` field, compute `ResponseCache.similarity()`, find best above threshold
4. Apply false-hit check before returning

**Verify:** `pytest tests/test_redis_cache.py -v` → 6/6 passing

Switch config to `backend: redis` and re-run chaos to verify Redis-backed cache works.

---

### Phase 6: Report (270–300 min) — 15 points

Copy `reports/report_template.md` → `reports/final_report.md`. Fill ALL sections:

1. **Architecture diagram** — text/ASCII showing: User → Gateway → Cache → Circuit Breaker → Provider chain → Static fallback
2. **Config table** — every parameter + rationale (e.g., "similarity_threshold: 0.92 — tested 0.85, got false hits on date queries")
3. **Metrics** — paste from `metrics.json`, must include P50/P95/P99, availability, cache metrics
4. **Chaos scenarios** — expected vs. observed, pass/fail per scenario
5. **Cache comparison** — with vs. without cache (run twice, different config)
6. **Redis evidence** — shared state proof, `KEYS` output
7. **Failure analysis** — one remaining weakness + proposed fix

---

## Rubric (100 points)

| Category | Points | What graders check |
|---|---:|---|
| Circuit breaker & fallback | 25 | Correct 3-state machine, no retry storm, route reasons include provider name, transition log |
| In-memory cache & cost | 15 | Hit rate measured, cost saved calculated, TTL/threshold justified, false-hit examples shown |
| Redis shared cache | 15 | SharedRedisCache get/set implemented, shared state verified, privacy guardrails, Redis tests pass |
| Observability & metrics | 15 | metrics.json has P50/P95/P99, availability, circuit open count, cache metrics, all reproducible |
| Chaos & load testing | 15 | At least 3 named scenarios with pass/fail, recovery evidence, cache comparison |
| Report & code quality | 15 | Architecture diagram, config table with justifications, failure analysis, type hints, tests pass |

---

## Required deliverables

Submit a zip or GitHub repo containing:

1. **Source code** — all TODOs completed in `src/reliability_lab/`
2. **`reports/metrics.json`** — generated by `make run-chaos`, reproducible
3. **`reports/final_report.md`** — all sections filled
4. **Test output** — screenshot/log of `make test` passing (with Redis running)
5. **`docker-compose.yml`** — so grader can start Redis

Grader runs:
```bash
pip install -e ".[dev]"
docker compose up -d
make test         # all tests pass
make run-chaos    # generates metrics
make report       # generates report
```

---

## Stretch goals (extra credit)

- **Concurrency**: `ThreadPoolExecutor` in `run_simulation` — show metrics differ under concurrent load
- **Redis circuit state**: Store breaker counters in Redis (INCR, EXPIRE) for multi-instance state sharing
- **Redis graceful degradation**: Fall back to in-memory cache if Redis is down
- **Cost-aware routing**: After budget hits 80%, route to cheaper model; at 100%, cache-only or static
- **Property-based tests**: Use `hypothesis` to fuzz circuit breaker state transitions
- **SLO table**: Define SLOs (availability >= 99%, P95 < 2.5s), check if system meets them

### Implemented in this submission (see `reports/final_report.md` §10)

| Stretch goal | Where | Run it |
|---|---|---|
| Concurrency (`ThreadPoolExecutor`) + breaker locks | `chaos.run_scenario_concurrent`, `CircuitBreaker._lock` | `python scripts/run_concurrent.py` |
| Redis circuit state (shared, single-flight probe) | `circuit_breaker.SharedRedisCircuitBreaker`, `circuit_breaker.backend: redis` | `python scripts/redis_circuit_demo.py` · `configs/redis_full.yaml` |
| Cost-aware routing (80% → cheapest first, 100% → cache/static) | `gateway.ReliabilityGateway` `budget_usd`, `routing.budget_usd` | `python scripts/cost_aware_demo.py` |
| Property-based tests (hypothesis) | `tests/test_circuit_breaker_properties.py` | `pytest tests/test_circuit_breaker_properties.py` |
| SLO table | `reports/final_report.md` §3 | — |
| Deterministic chaos (virtual clock) | `chaos.ManualClock`, injectable `CircuitBreaker.clock` | two `make run-chaos` → identical counters |

---

## Common mistakes

| Mistake | Points lost | How to avoid |
|---|---:|---|
| Report has no numbers, only descriptions | Up to 20 | Always paste actual `metrics.json` values |
| `record_failure()` uses `or` instead of `if/elif` | Up to 10 | HALF_OPEN and threshold need different reasons |
| Cache returns wrong answers without guardrails | Up to 10 | Add privacy check + false-hit detection |
| Similarity uses Jaccard instead of n-gram cosine | Up to 5 | Token overlap too crude — implement character n-grams |
| Only 1 chaos scenario | Up to 10 | Implement 3+ scenarios with different failure modes |
| Redis tests skipped | Up to 10 | Always `make docker-up` before `make test` |
| Code doesn't run on grader's machine | Up to 15 | Test: fresh env → `pip install -e ".[dev]" && make test` |
| No type hints | Up to 5 | Run `make typecheck` before submission |

## FAQ

**Q: Do I need real API keys?**
No. `FakeLLMProvider` simulates everything locally.

**Q: What order should I implement things?**
Circuit breaker → Cache → Gateway → Chaos/Metrics → Redis. Each layer builds on previous.

**Q: What if I can't get Docker/Redis working?**
Implement everything else first — Redis is 15 points. You can install Redis natively (`brew install redis` on Mac).

**Q: Can I use external libraries?**
Yes — add to `pyproject.toml`. Common: `scikit-learn` (TF-IDF), `hypothesis` (property tests).

**Q: The `test_todo_requirements.py` tests are xfail — do they matter?**
They flip to unexpected PASS when your code is correct. Graders check that xfails become passes.

**Q: How do I know I'm done?**
`make test` shows 0 failures (xfails that pass = good). `make run-chaos` produces valid `metrics.json`.
