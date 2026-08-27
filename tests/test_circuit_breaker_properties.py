"""Property-based tests for the circuit breaker state machine (stretch goal).

These fuzz arbitrary sequences of successes / failures / time advances and
assert invariants that must hold for *every* sequence, not just the handful of
hand-written cases in test_circuit_breaker.py.
"""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from reliability_lab.circuit_breaker import CircuitBreaker, CircuitState

_VALID_STATES = set(CircuitState)


class FakeClock:
    """Manually advanced monotonic clock."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _make(failure_threshold: int = 3, reset: float = 1.0, success_threshold: int = 1) -> tuple[CircuitBreaker, FakeClock]:
    clk = FakeClock()
    cb = CircuitBreaker(
        "prop",
        failure_threshold=failure_threshold,
        reset_timeout_seconds=reset,
        success_threshold=success_threshold,
        clock=clk,
    )
    return cb, clk


# --- invariants over arbitrary event sequences ---------------------------

_EVENTS = st.lists(
    st.sampled_from(["ok", "fail", "tick"]),
    max_size=60,
)


@given(threshold=st.integers(min_value=1, max_value=6), events=_EVENTS)
@settings(max_examples=250)
def test_state_always_valid_and_counts_nonnegative(threshold: int, events: list[str]) -> None:
    cb, clk = _make(failure_threshold=threshold, reset=1.0)
    for ev in events:
        if ev == "ok":
            cb.record_success()
        elif ev == "fail":
            cb.record_failure()
        else:
            clk.advance(0.5)
            cb.allow_request()
        assert cb.state in _VALID_STATES
        assert cb.failure_count >= 0
        assert cb.success_count >= 0


@given(events=_EVENTS)
@settings(max_examples=250)
def test_transition_log_is_a_well_formed_chain(events: list[str]) -> None:
    cb, clk = _make(failure_threshold=2, reset=1.0)
    for ev in events:
        if ev == "ok":
            cb.record_success()
        elif ev == "fail":
            cb.record_failure()
        else:
            clk.advance(0.6)
            cb.allow_request()

    log = cb.transition_log
    for entry in log:
        assert entry["from"] in {s.value for s in CircuitState}
        assert entry["to"] in {s.value for s in CircuitState}
        assert entry["from"] != entry["to"]  # _transition drops no-ops
    for prev, nxt in zip(log, log[1:]):
        assert prev["to"] == nxt["from"]  # states chain
    if log:
        assert cb.state.value == log[-1]["to"]


# --- targeted properties ----------------------------------------------------

@given(n=st.integers(min_value=0, max_value=20), threshold=st.integers(min_value=1, max_value=20))
def test_below_threshold_never_opens(n: int, threshold: int) -> None:
    cb, _ = _make(failure_threshold=threshold, reset=1.0)
    for _ in range(min(n, threshold - 1)):
        cb.record_failure()
    assert cb.state is CircuitState.CLOSED
    assert cb.allow_request() is True


@given(threshold=st.integers(min_value=1, max_value=10))
def test_exactly_threshold_consecutive_failures_opens(threshold: int) -> None:
    cb, _ = _make(failure_threshold=threshold, reset=1.0)
    for _ in range(threshold):
        cb.record_failure()
    assert cb.state is CircuitState.OPEN
    assert cb.allow_request() is False
    assert cb.transition_log[-1]["reason"] == "failure_threshold_reached"


@given(
    reset=st.floats(min_value=0.1, max_value=5.0),
    wait=st.floats(min_value=0.0, max_value=10.0),
)
def test_open_denies_until_reset_then_probes(reset: float, wait: float) -> None:
    cb, clk = _make(failure_threshold=1, reset=reset)
    cb.record_failure()
    assert cb.state is CircuitState.OPEN
    clk.advance(wait)
    allowed = cb.allow_request()
    if wait >= reset:
        assert allowed is True
        assert cb.state is CircuitState.HALF_OPEN
    else:
        assert allowed is False
        assert cb.state is CircuitState.OPEN


@given(pre_failures=st.integers(min_value=0, max_value=10))
def test_half_open_failure_always_reopens_with_probe_reason(pre_failures: int) -> None:
    cb, clk = _make(failure_threshold=3, reset=0.5)
    cb.state = CircuitState.HALF_OPEN
    cb.failure_count = pre_failures  # regardless of how many earlier failures
    cb.record_failure()
    assert cb.state is CircuitState.OPEN
    assert cb.transition_log[-1]["reason"] == "probe_failure"


@given(success_threshold=st.integers(min_value=1, max_value=5))
def test_half_open_closes_only_after_success_threshold(success_threshold: int) -> None:
    cb, _ = _make(failure_threshold=1, reset=0.5, success_threshold=success_threshold)
    cb.state = CircuitState.HALF_OPEN
    for i in range(success_threshold):
        assert cb.state is CircuitState.HALF_OPEN
        cb.record_success()
    assert cb.state is CircuitState.CLOSED
