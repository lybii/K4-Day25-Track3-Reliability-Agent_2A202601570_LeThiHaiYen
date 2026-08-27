"""Stretch goal: cost-aware routing under a spend budget.

Runs requests through a gateway with ``routing.budget_usd`` set very low so the
cap is hit mid-run, and prints how the route mix shifts:

  spend < 80% budget   -> normal order (primary first)
  80% <= spend < 100%  -> cheapest provider first  (route "cost_saver_*")
  spend >= 100%        -> no paid call at all: cache hit or "budget_exhausted"

Compares total spend and served rate against the same run with no budget.
"""
from __future__ import annotations

import argparse
import random
from collections import Counter

from reliability_lab.chaos import SIMULATION_SEED, build_gateway, load_queries
from reliability_lab.config import load_config


def _run(config: object, queries: list[str], label: str) -> None:
    random.seed(SIMULATION_SEED)
    gw = build_gateway(config)  # type: ignore[arg-type]
    routes: Counter[str] = Counter()
    served = 0
    for _ in range(config.load_test.requests):  # type: ignore[attr-defined]
        r = gw.complete(random.choice(queries))
        key = r.route.split(":")[0]
        routes[key] += 1
        if r.route not in ("static_fallback", "budget_exhausted"):
            served += 1
    budget = gw.budget_usd
    print(f"\n[{label}] budget={budget}  requests={config.load_test.requests}")  # type: ignore[attr-defined]
    print(f"  spend        = ${gw.cumulative_cost:.4f}")
    print(f"  served       = {served}/{config.load_test.requests}")  # type: ignore[attr-defined]
    for route, n in routes.most_common():
        print(f"  route {route:<22} {n}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/cost_cap.yaml")
    args = parser.parse_args()

    config = load_config(args.config)
    queries = load_queries()

    _run(config, queries, "cost cap ON")

    config.routing.budget_usd = None
    _run(config, queries, "cost cap OFF (same run, no budget)")


if __name__ == "__main__":
    main()
