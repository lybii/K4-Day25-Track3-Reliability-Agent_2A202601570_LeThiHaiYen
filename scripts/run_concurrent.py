"""Stretch goal: serial vs concurrent load, to show the metrics move.

Runs one scenario twice against an identical gateway config:
  1. serial  (workers=1)
  2. concurrent (workers=N from --workers or load_test.concurrency)

The FakeLLMProvider sleeps for its latency, so threads genuinely overlap and
the circuit breakers take real concurrent hits — this exercises their locks.

Output: a small comparison table (wall time, throughput, P95, circuit opens).
"""
from __future__ import annotations

import argparse
import time

from reliability_lab.chaos import load_queries, run_scenario, run_scenario_concurrent
from reliability_lab.config import ScenarioConfig, load_config


def _row(label: str, metrics: object, wall_s: float) -> str:
    m = metrics
    thr = m.total_requests / wall_s if wall_s else 0.0  # type: ignore[attr-defined]
    return (
        f"{label:<12} wall={wall_s:6.2f}s  throughput={thr:6.1f} req/s  "
        f"avail={m.availability:.3f}  P95={m.percentile(95):7.1f}ms  "  # type: ignore[attr-defined]
        f"circuit_opens={m.circuit_open_count:>3}  "  # type: ignore[attr-defined]
        f"fallback_sr={m.fallback_success_rate:.3f}"  # type: ignore[attr-defined]
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/default.yaml")
    parser.add_argument("--scenario", default="primary_flaky_55")
    parser.add_argument("--workers", type=int, default=0, help="0 = use load_test.concurrency")
    args = parser.parse_args()

    config = load_config(args.config)
    queries = load_queries()
    workers = args.workers or config.load_test.concurrency
    if workers < 2:
        workers = 8

    scenario = next(
        (s for s in config.scenarios if s.name == args.scenario),
        ScenarioConfig(name=args.scenario, description="ad-hoc"),
    )

    t0 = time.perf_counter()
    serial = run_scenario(config, queries, scenario)
    serial_wall = time.perf_counter() - t0

    t0 = time.perf_counter()
    concurrent = run_scenario_concurrent(config, queries, scenario, workers)
    concurrent_wall = time.perf_counter() - t0

    print(f"scenario: {scenario.name}   requests: {config.load_test.requests}   workers: {workers}")
    print(_row("serial", serial, serial_wall))
    print(_row("concurrent", concurrent, concurrent_wall))
    speedup = serial_wall / concurrent_wall if concurrent_wall else 0.0
    print(f"\nwall-clock speedup: {speedup:.1f}x")
    print(
        "note: concurrent circuit_opens / P95 differ from serial because many "
        "requests now hit a still-OPEN breaker in the same window, and probes "
        "race -- the locks keep the counters consistent, not identical."
    )


if __name__ == "__main__":
    main()
