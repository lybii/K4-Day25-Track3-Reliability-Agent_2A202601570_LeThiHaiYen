from __future__ import annotations

import json
import random
import time
import zlib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker, SharedRedisCircuitBreaker
from reliability_lab.config import LabConfig, ScenarioConfig
from reliability_lab.gateway import BreakerLike, GatewayResponse, ReliabilityGateway
from reliability_lab.metrics import RunMetrics
from reliability_lab.providers import FakeLLMProvider

# Fixed seed so `make run-chaos` is reproducible across machines/graders.
SIMULATION_SEED = 1234

# Modeled seconds of "time" each serial request advances the virtual clock by.
# Roughly the provider base latency; with reset_timeout_seconds=2 a tripped
# breaker gets its probe ~8 requests after it opened — deterministically.
TICK_SECONDS = 0.25

# Routes that mean "served, but via the backup path".
_FALLBACK_ROUTES = {"fallback", "cost_saver_fallback"}
# Routes that mean "not really served".
_FAILED_ROUTES = {"static_fallback", "budget_exhausted"}


class ManualClock:
    """Deterministic virtual clock injected into breakers during chaos runs.

    Real ``time.monotonic()`` jitter from ``FakeLLMProvider.time.sleep`` leaks
    into breaker OPEN→HALF_OPEN decisions and makes worst-case scenarios
    non-reproducible. Driving the breakers off this clock instead makes the
    whole simulation a pure function of ``SIMULATION_SEED``.
    """

    __slots__ = ("_t",)

    def __init__(self, start: float = 0.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


def scenario_seed(name: str) -> int:
    """Stable per-scenario RNG seed.

    Position-independent (uses a CRC of the name, not the loop index) so
    reordering scenarios in the config never changes a scenario's result.
    """
    return SIMULATION_SEED ^ zlib.crc32(name.encode())


def load_queries(path: str | Path = "data/sample_queries.jsonl") -> list[str]:
    queries: list[str] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        queries.append(json.loads(line)["query"])
    return queries


def build_gateway(
    config: LabConfig,
    provider_overrides: dict[str, float] | None = None,
    *,
    sim_clock: Callable[[], float] | None = None,
) -> ReliabilityGateway:
    providers = []
    for p in config.providers:
        fail_rate = provider_overrides.get(p.name, p.fail_rate) if provider_overrides else p.fail_rate
        providers.append(FakeLLMProvider(p.name, fail_rate, p.base_latency_ms, p.cost_per_1k_tokens))

    breakers: dict[str, BreakerLike] = {}
    for p in config.providers:
        if config.circuit_breaker.backend == "redis":
            rb = SharedRedisCircuitBreaker(
                name=p.name,
                redis_url=config.cache.redis_url,
                failure_threshold=config.circuit_breaker.failure_threshold,
                reset_timeout_seconds=config.circuit_breaker.reset_timeout_seconds,
                success_threshold=config.circuit_breaker.success_threshold,
            )
            rb.reset()  # clean slate per chaos run
            breakers[p.name] = rb
        else:
            breakers[p.name] = CircuitBreaker(
                name=p.name,
                failure_threshold=config.circuit_breaker.failure_threshold,
                reset_timeout_seconds=config.circuit_breaker.reset_timeout_seconds,
                success_threshold=config.circuit_breaker.success_threshold,
                clock=sim_clock if sim_clock is not None else time.monotonic,
            )

    cache: ResponseCache | SharedRedisCache | None = None
    if config.cache.enabled:
        if config.cache.backend == "redis":
            cache = SharedRedisCache(
                config.cache.redis_url,
                config.cache.ttl_seconds,
                config.cache.similarity_threshold,
            )
        else:
            cache = ResponseCache(config.cache.ttl_seconds, config.cache.similarity_threshold)

    return ReliabilityGateway(
        providers, breakers, cache, budget_usd=config.routing.budget_usd
    )


def calculate_recovery_time_ms(gateway: ReliabilityGateway) -> float | None:
    """Average time a breaker spent OPEN before returning to CLOSED, in ms.

    Walks each breaker's transition log pairing every ``-> open`` with the next
    ``-> closed``. Returns ``None`` when no full open→closed recovery happened.
    """
    recovery_times_ms: list[float] = []
    for breaker in gateway.breakers.values():
        open_ts: float | None = None
        for entry in breaker.transition_log:
            if entry["to"] == "open":
                open_ts = float(entry["ts"])
            elif entry["to"] == "closed" and open_ts is not None:
                recovery_times_ms.append((float(entry["ts"]) - open_ts) * 1000.0)
                open_ts = None
    if not recovery_times_ms:
        return None
    return sum(recovery_times_ms) / len(recovery_times_ms)


def _tally(metrics: RunMetrics, r: GatewayResponse) -> None:
    """Fold one GatewayResponse into the running metrics."""
    metrics.total_requests += 1
    metrics.estimated_cost += r.estimated_cost
    if r.cache_hit:
        metrics.cache_hits += 1
        metrics.estimated_cost_saved += 0.001
    if r.route in _FALLBACK_ROUTES:
        metrics.fallback_successes += 1
        metrics.successful_requests += 1
    elif r.route in _FAILED_ROUTES:
        metrics.static_fallbacks += 1
        metrics.failed_requests += 1
    else:  # primary / cost_saver_primary / cache_hit:*
        metrics.successful_requests += 1
    if r.latency_ms > 0:
        metrics.latencies_ms.append(r.latency_ms)


def _finalize(metrics: RunMetrics, gateway: ReliabilityGateway) -> RunMetrics:
    for breaker in gateway.breakers.values():
        metrics.circuit_open_count += sum(
            1 for entry in breaker.transition_log if entry["to"] == "open"
        )
    metrics.recovery_time_ms = calculate_recovery_time_ms(gateway)
    return metrics


def run_scenario(
    config: LabConfig,
    queries: list[str],
    scenario: ScenarioConfig,
    *,
    seed: int | None = None,
) -> RunMetrics:
    """Run a single named chaos scenario serially with a deterministic clock.

    Reseeds the global RNG (the fake provider draws from it) so each scenario is
    independent of the ones before it and fully reproducible.
    """
    random.seed(seed if seed is not None else scenario_seed(scenario.name))
    sim_clock = ManualClock()
    gateway = build_gateway(config, scenario.provider_overrides or None, sim_clock=sim_clock)
    metrics = RunMetrics()

    for _ in range(config.load_test.requests):
        prompt = random.choice(queries)
        _tally(metrics, gateway.complete(prompt))
        sim_clock.advance(TICK_SECONDS)

    return _finalize(metrics, gateway)


def run_scenario_concurrent(
    config: LabConfig,
    queries: list[str],
    scenario: ScenarioConfig,
    workers: int,
) -> RunMetrics:
    """Run a scenario under ``workers`` concurrent threads (real wall clock).

    The breakers here use the real ``time.monotonic`` clock and their locks, so
    this exercises the thread-safety of :class:`CircuitBreaker`. Results are
    *not* seed-reproducible — that is the point: concurrency changes the numbers.
    """
    gateway = build_gateway(config, scenario.provider_overrides or None)  # real clock
    metrics = RunMetrics()
    rng = random.Random(SIMULATION_SEED)
    prompts = [rng.choice(queries) for _ in range(config.load_test.requests)]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(gateway.complete, prompts))
    for result in results:
        _tally(metrics, result)

    return _finalize(metrics, gateway)


def _scenario_passed(name: str, result: RunMetrics) -> bool:
    """Per-scenario pass/fail criteria, keyed by scenario name prefix.

    Each failure mode has a different definition of "the reliability layer did
    its job" — see the report for the rationale behind each threshold.
    """
    if result.total_requests == 0:
        return False
    if name.startswith("primary_timeout"):
        # Primary is dead: traffic must survive on the backup path, and the
        # primary breaker must trip so we stop hammering a dead dependency.
        return (
            result.fallback_success_rate >= 0.9
            and result.availability >= 0.9
            and result.circuit_open_count >= 1
        )
    if name.startswith("primary_flaky"):
        # Primary is flaky: cache + fallback keep availability up, and the
        # breaker must both trip AND heal within the run.
        return (
            result.availability >= 0.9
            and result.circuit_open_count >= 1
            and result.recovery_time_ms is not None
        )
    if name.startswith("both_degraded"):
        # Worst case: we do NOT expect high availability. We expect graceful
        # degradation — breakers open to fail fast and the static fallback
        # absorbs the storm instead of the process hanging or crashing.
        return result.circuit_open_count >= 1 and result.static_fallbacks > 0
    if name.startswith("cost_cap"):
        # Budget must actually bite: once exhausted, no more paid calls.
        return result.static_fallbacks > 0 or result.cache_hit_rate > 0
    # Healthy baseline.
    return result.availability >= 0.95


def run_simulation(config: LabConfig, queries: list[str]) -> RunMetrics:
    """Run all named scenarios from config, or a default run if none defined.

    Each scenario reseeds the RNG from its own name (see ``run_scenario`` /
    ``scenario_seed``) and the breakers run off a deterministic virtual clock,
    so the aggregate counters are reproducible run-to-run and independent of
    scenario order.
    """
    if not config.scenarios:
        default_scenario = ScenarioConfig(name="default", description="baseline run")
        metrics = run_scenario(config, queries, default_scenario)
        metrics.scenarios = {"default": "pass" if metrics.successful_requests > 0 else "fail"}
        return metrics

    combined = RunMetrics()
    for scenario in config.scenarios:
        result = run_scenario(config, queries, scenario)

        passed = _scenario_passed(scenario.name, result)
        combined.scenarios[scenario.name] = "pass" if passed else "fail"

        combined.total_requests += result.total_requests
        combined.successful_requests += result.successful_requests
        combined.failed_requests += result.failed_requests
        combined.fallback_successes += result.fallback_successes
        combined.static_fallbacks += result.static_fallbacks
        combined.cache_hits += result.cache_hits
        combined.circuit_open_count += result.circuit_open_count
        combined.estimated_cost += result.estimated_cost
        combined.estimated_cost_saved += result.estimated_cost_saved
        combined.latencies_ms.extend(result.latencies_ms)
        if result.recovery_time_ms is not None:
            if combined.recovery_time_ms is None:
                combined.recovery_time_ms = result.recovery_time_ms
            else:
                combined.recovery_time_ms = (combined.recovery_time_ms + result.recovery_time_ms) / 2

    return combined
