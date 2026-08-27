"""Stretch goal: circuit-breaker state shared across instances via Redis.

Two SharedRedisCircuitBreaker objects (A and B) for the same provider name,
each standing in for a separate gateway replica pointed at the same Redis.
A trips the breaker; B sees OPEN immediately and fails fast without ever
making a bad call itself; after the reset timeout exactly one of them wins
the single-flight probe.
"""
from __future__ import annotations

import time

from reliability_lab.circuit_breaker import CircuitOpenError, SharedRedisCircuitBreaker

URL = "redis://localhost:6379/0"
RESET = 2.0


def _mk() -> SharedRedisCircuitBreaker:
    return SharedRedisCircuitBreaker(
        name="primary",
        redis_url=URL,
        failure_threshold=3,
        reset_timeout_seconds=RESET,
        success_threshold=1,
        prefix="rl:cbdemo:",
    )


def main() -> None:
    a = _mk()  # replica A
    b = _mk()  # replica B — separate object, separate connection, same Redis
    a.reset()

    print("start:                A.state =", a.state.value, " B.state =", b.state.value)

    # Replica A takes 3 failures.
    for _ in range(3):
        a.record_failure()
    print(
        f"after 3 failures on A: A.state = {a.state.value}  B.state = {b.state.value}  "
        f"(B never saw a failure)"
    )

    # Replica B now fails fast on the shared state.
    try:
        b.call(lambda: "should not run")
    except CircuitOpenError as exc:
        print(f"B.call(...) -> fails fast: {exc}")
    print(f"B.allow_request() = {b.allow_request()}  (shared OPEN)")

    # Wait out the cooldown; exactly one replica wins the probe.
    time.sleep(RESET + 0.2)
    a_probe = a.allow_request()
    b_probe = b.allow_request()
    print(
        f"after cooldown: A.allow_request()={a_probe}  B.allow_request()={b_probe}  "
        f"(single-flight: exactly one True)"
    )
    assert a_probe != b_probe, "single-flight probe lock failed"

    # The winner's success closes the breaker for everyone.
    winner = a if a_probe else b
    winner.record_success()
    print(f"probe success -> A.state={a.state.value}  B.state={b.state.value}  (shared CLOSED)")
    print("transition_log (shared, read from Redis):")
    for e in a.transition_log:
        print(f"   {e['from']:>9} -> {e['to']:<9} {e['reason']}")

    a.reset()


if __name__ == "__main__":
    main()
