"""Run each configured chaos scenario in isolation and dump per-scenario metrics.

Unlike `run_chaos.py` (which aggregates every scenario into one RunMetrics), this
writes one row per scenario so the report can show expected-vs-observed per
failure mode. Output: reports/scenarios.json  +  reports/scenarios.csv
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from reliability_lab.chaos import _scenario_passed, load_queries, run_scenario
from reliability_lab.config import load_config


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/default.yaml")
    parser.add_argument("--out", default="reports/scenarios.json")
    args = parser.parse_args()

    config = load_config(args.config)
    queries = load_queries()

    rows: list[dict[str, object]] = []
    for scenario in config.scenarios:
        m = run_scenario(config, queries, scenario)
        rows.append(
            {
                "scenario": scenario.name,
                "description": scenario.description,
                "total_requests": m.total_requests,
                "availability": round(m.availability, 4),
                "error_rate": round(m.error_rate, 4),
                "latency_p50_ms": round(m.percentile(50), 2),
                "latency_p95_ms": round(m.percentile(95), 2),
                "latency_p99_ms": round(m.percentile(99), 2),
                "fallback_success_rate": round(m.fallback_success_rate, 4),
                "cache_hit_rate": round(m.cache_hit_rate, 4),
                "fallback_successes": m.fallback_successes,
                "static_fallbacks": m.static_fallbacks,
                "cache_hits": m.cache_hits,
                "circuit_open_count": m.circuit_open_count,
                "recovery_time_ms": (
                    round(m.recovery_time_ms, 2) if m.recovery_time_ms is not None else None
                ),
                "estimated_cost": round(m.estimated_cost, 6),
                "estimated_cost_saved": round(m.estimated_cost_saved, 6),
                "result": "pass" if _scenario_passed(scenario.name, m) else "fail",
            }
        )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")

    csv_out = out.with_suffix(".csv")
    with open(csv_out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    print(f"wrote {out}")
    print(f"wrote {csv_out}")
    for row in rows:
        print(
            f"  {row['scenario']:<20} "
            f"avail={row['availability']} "
            f"fallback_sr={row['fallback_success_rate']} "
            f"cache_hit={row['cache_hit_rate']} "
            f"open={row['circuit_open_count']} "
            f"recovery_ms={row['recovery_time_ms']} "
            f"-> {row['result']}"
        )


if __name__ == "__main__":
    main()
