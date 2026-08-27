from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TypeVar

T = TypeVar("T")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised when a circuit is open and calls should fail fast."""


@dataclass(slots=True)
class CircuitBreaker:
    """Production-safe circuit breaker state machine.

    - CLOSED: calls pass through; consecutive failures are counted.
    - OPEN: fail fast until ``reset_timeout_seconds`` elapses.
    - HALF_OPEN: allow a single probe; close on success, re-open on failure.

    Thread-safe: ``allow_request`` / ``record_success`` / ``record_failure`` each
    take a re-entrant lock around the state mutation (never around the wrapped
    call itself, so concurrent requests still run in parallel).

    ``clock`` is injectable purely so tests and the chaos simulator can drive
    time deterministically; production leaves it as ``time.monotonic``.
    """

    name: str
    failure_threshold: int
    reset_timeout_seconds: float
    success_threshold: int = 1
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    opened_at: float | None = None
    transition_log: list[dict[str, str | float]] = field(default_factory=list)
    clock: Callable[[], float] = time.monotonic
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)

    def allow_request(self) -> bool:
        """Return whether a request should be attempted.

        - CLOSED    → always allow.
        - HALF_OPEN → allow a single probe request through.
        - OPEN      → fail fast until ``reset_timeout_seconds`` has elapsed since
          ``opened_at``; once it has, move to HALF_OPEN and allow the probe.
        """
        with self._lock:
            if self.state == CircuitState.CLOSED:
                return True
            if self.state == CircuitState.HALF_OPEN:
                return True
            # OPEN: check whether the cooldown has elapsed.
            if self.opened_at is not None and (
                self.clock() - self.opened_at >= self.reset_timeout_seconds
            ):
                self._transition(CircuitState.HALF_OPEN, "reset_timeout_elapsed")
                return True
            return False

    def call(self, fn: Callable[..., T], *args: object, **kwargs: object) -> T:
        """Call ``fn`` through the breaker, recording the outcome.

        Raises :class:`CircuitOpenError` without calling ``fn`` when the breaker
        is OPEN and still cooling down.
        """
        if not self.allow_request():
            raise CircuitOpenError(f"circuit '{self.name}' is open")
        try:
            result = fn(*args, **kwargs)
        except Exception:
            self.record_failure()
            raise
        self.record_success()
        return result

    def record_success(self) -> None:
        """Record a successful call and close the circuit after enough probes."""
        with self._lock:
            self.failure_count = 0
            self.success_count += 1
            if (
                self.state == CircuitState.HALF_OPEN
                and self.success_count >= self.success_threshold
            ):
                self._transition(CircuitState.CLOSED, "probe_success")
                self.success_count = 0

    def record_failure(self) -> None:
        """Record a failed call and open the circuit when warranted.

        A failure during a HALF_OPEN probe re-opens immediately with a distinct
        reason; only in other states does the failure_threshold apply. These are
        separate ``if``/``elif`` branches on purpose — different triggers, different
        reasons in the transition log.
        """
        with self._lock:
            self.failure_count += 1
            self.success_count = 0
            if self.state == CircuitState.HALF_OPEN:
                self._transition(CircuitState.OPEN, "probe_failure")
                self.opened_at = self.clock()
            elif self.failure_count >= self.failure_threshold:
                self._transition(CircuitState.OPEN, "failure_threshold_reached")
                self.opened_at = self.clock()

    def _transition(self, new_state: CircuitState, reason: str) -> None:
        if self.state == new_state:
            return
        self.transition_log.append(
            {"from": self.state.value, "to": new_state.value, "reason": reason, "ts": self.clock()}
        )
        self.state = new_state


# ---------------------------------------------------------------------------
# Redis-backed circuit breaker — shared trip state across gateway instances
# ---------------------------------------------------------------------------


class SharedRedisCircuitBreaker:
    """Circuit breaker whose state lives in Redis, shared by every instance.

    Solves the per-process weakness of :class:`CircuitBreaker`: when one gateway
    replica trips the breaker, every other replica sees OPEN immediately and
    fails fast, and a restarted replica inherits the current state instead of
    re-discovering a dead dependency.

    Redis layout (prefix ``rl:cb:<name>:``):
        ``state``      String  "open" | "half_open"   (absent ⇒ closed)
        ``opened_at``  String  Redis-clock seconds when it last opened
        ``failures``   String  INCR counter, sliding TTL
        ``successes``  String  INCR counter during a HALF_OPEN probe
        ``probe``      String  SET NX EX lock — single-flight probe across replicas
        ``log``        List    JSON transition entries (RPUSH), read via LRANGE

    All replicas read time from ``redis.time()`` so cooldown math is consistent.
    Each public method is a short sequence of Redis calls; it is *not* a single
    atomic transaction (a Lua script would make it so) — acceptable here because
    the single-flight ``probe`` lock covers the one race that matters.
    """

    def __init__(
        self,
        name: str,
        redis_url: str,
        failure_threshold: int,
        reset_timeout_seconds: float,
        success_threshold: int = 1,
        prefix: str = "rl:cb:",
    ):
        import uuid

        import redis as redis_lib
        from redis.backoff import ExponentialBackoff
        from redis.retry import Retry

        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_timeout_seconds = reset_timeout_seconds
        self.success_threshold = success_threshold
        self._p = f"{prefix}{name}:"
        self._id = uuid.uuid4().hex  # identifies this replica when it holds the probe
        self._window = max(int(reset_timeout_seconds * 4), 5)
        self._redis: Any = redis_lib.Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=10,
            socket_timeout=10,
            retry=Retry(ExponentialBackoff(cap=1.0, base=0.2), retries=3),
            retry_on_timeout=True,
            retry_on_error=[redis_lib.exceptions.ConnectionError, redis_lib.exceptions.TimeoutError],
        )

    # -- helpers -----------------------------------------------------------
    def _now(self) -> float:
        secs, micros = self._redis.time()
        return float(secs) + float(micros) / 1_000_000.0

    def _raw_state(self) -> str:
        return self._redis.get(f"{self._p}state") or "closed"

    def _log(self, new_state: str, reason: str) -> None:
        entry = {
            "from": self._raw_state(),
            "to": new_state,
            "reason": reason,
            "ts": self._now(),
        }
        self._redis.rpush(f"{self._p}log", json.dumps(entry))

    def _open(self, reason: str) -> None:
        self._log("open", reason)
        self._redis.set(f"{self._p}state", "open", ex=self._window)
        self._redis.set(f"{self._p}opened_at", self._now(), ex=self._window)
        self._redis.delete(f"{self._p}probe", f"{self._p}successes")

    # -- public API (mirrors CircuitBreaker) -----------------------------
    @property
    def state(self) -> CircuitState:
        return CircuitState(self._raw_state())

    @property
    def failure_count(self) -> int:
        return int(self._redis.get(f"{self._p}failures") or 0)

    @property
    def transition_log(self) -> list[dict[str, Any]]:
        return [json.loads(x) for x in self._redis.lrange(f"{self._p}log", 0, -1)]

    def allow_request(self) -> bool:
        st = self._raw_state()
        if st == "closed":
            return True
        if st == "half_open":
            # Only the replica holding the probe lock may send the probe;
            # every other replica keeps failing fast.
            return bool(self._redis.get(f"{self._p}probe") == self._id)
        # OPEN: has the cooldown elapsed on the shared clock?
        opened_at = float(self._redis.get(f"{self._p}opened_at") or 0.0)
        if self._now() - opened_at < self.reset_timeout_seconds:
            return False
        # Single-flight: exactly one replica wins the probe lock.
        if self._redis.set(
            f"{self._p}probe", self._id, nx=True, ex=int(self.reset_timeout_seconds) + 1
        ):
            self._log("half_open", "reset_timeout_elapsed")
            self._redis.set(f"{self._p}state", "half_open", ex=self._window)
            return True
        return False

    def call(self, fn: Callable[..., T], *args: object, **kwargs: object) -> T:
        if not self.allow_request():
            raise CircuitOpenError(f"circuit '{self.name}' is open (shared)")
        try:
            result = fn(*args, **kwargs)
        except Exception:
            self.record_failure()
            raise
        self.record_success()
        return result

    def record_success(self) -> None:
        st = self._raw_state()
        if st == "half_open":
            got = self._redis.incr(f"{self._p}successes")
            if got >= self.success_threshold:
                self._log("closed", "probe_success")
                self._redis.delete(
                    f"{self._p}state",
                    f"{self._p}opened_at",
                    f"{self._p}failures",
                    f"{self._p}successes",
                    f"{self._p}probe",
                )
        else:
            self._redis.delete(f"{self._p}failures")

    def record_failure(self) -> None:
        n = self._redis.incr(f"{self._p}failures")
        self._redis.expire(f"{self._p}failures", self._window)
        st = self._raw_state()
        if st == "half_open":
            self._open("probe_failure")
        elif n >= self.failure_threshold:
            self._open("failure_threshold_reached")

    def reset(self) -> None:
        """Clear all keys for this breaker (test helper)."""
        for key in self._redis.scan_iter(f"{self._p}*"):
            self._redis.delete(key)
