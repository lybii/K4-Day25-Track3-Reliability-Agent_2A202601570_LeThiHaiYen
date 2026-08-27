"""Prove that two independent SharedRedisCache instances share state via Redis.

Instance A writes; instance B (a separate object, separate connection, as if a
second gateway process) reads the same value back. Also shows the privacy
guardrail and the false-hit guardrail working against the shared store.
"""
from __future__ import annotations

from reliability_lab.cache import SharedRedisCache

URL = "redis://localhost:6379/0"
PREFIX = "rl:demo:"


def main() -> None:
    a = SharedRedisCache(URL, ttl_seconds=60, similarity_threshold=0.6, prefix=PREFIX)
    b = SharedRedisCache(URL, ttl_seconds=60, similarity_threshold=0.6, prefix=PREFIX)
    a.flush()

    # 1. Shared state: A writes, B reads.
    a.set("Explain circuit breaker states in one paragraph.", "[A] CLOSED/OPEN/HALF_OPEN ...")
    val, score = b.get("Explain circuit breaker states in one paragraph.")
    print(f"1. shared exact-hit via instance B : value={val!r} score={score}")

    # 2. Shared semantic hit across instances.
    val, score = b.get("Explain the circuit breaker states in a paragraph")
    print(f"2. shared semantic hit via B       : value={val!r} score={round(score, 3)}")

    # 3. Privacy guardrail: never stored.
    a.set("Give me the current account balance for user 123.", "Balance: $500")
    val, _ = b.get("Give me the current account balance for user 123.")
    print(f"3. privacy query via B             : value={val!r} (expected None)")

    # 4. False-hit guardrail: different year -> reject + log.
    a.set("What is the tuition fee for the 2024 academic year?", "[A] 2024 tuition ...")
    val, score = b.get("What is the tuition fee for the 2025 academic year?")
    print(f"4. false-hit (2024 vs 2025) via B  : value={val!r} score={round(score, 3)} "
          f"log_len={len(b.false_hit_log)}")

    a.flush()
    a.close()
    b.close()


if __name__ == "__main__":
    main()
