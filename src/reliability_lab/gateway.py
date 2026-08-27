from __future__ import annotations

from dataclasses import dataclass

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
    SharedRedisCircuitBreaker,
)
from reliability_lab.providers import FakeLLMProvider, ProviderError

_STATIC_DEGRADED = "The service is temporarily degraded. Please try again soon."

BreakerLike = CircuitBreaker | SharedRedisCircuitBreaker


@dataclass(slots=True)
class GatewayResponse:
    text: str
    route: str
    provider: str | None
    cache_hit: bool
    latency_ms: float
    estimated_cost: float
    error: str | None = None


class ReliabilityGateway:
    """Routes requests through cache, circuit breakers, and fallback providers."""

    def __init__(
        self,
        providers: list[FakeLLMProvider],
        breakers: dict[str, BreakerLike],
        cache: ResponseCache | SharedRedisCache | None = None,
        *,
        budget_usd: float | None = None,
    ):
        self.providers = providers
        self.breakers = breakers
        self.cache = cache
        # Cost-aware routing: None disables it. Tracks spend across all calls
        # made through this gateway instance.
        self.budget_usd = budget_usd
        self.cumulative_cost = 0.0

    def _provider_order(self) -> tuple[list[FakeLLMProvider], str]:
        """Return the provider chain to try, plus the routing mode.

        - normal    : configured order.
        - cost_saver: >=80% of budget spent — cheapest provider first.
        """
        if self.budget_usd is not None and self.cumulative_cost >= 0.8 * self.budget_usd:
            return sorted(self.providers, key=lambda p: p.cost_per_1k_tokens), "cost_saver"
        return self.providers, "normal"

    def complete(self, prompt: str) -> GatewayResponse:
        """Return a reliable response or a static fallback.

        Pipeline: cache → provider chain guarded by circuit breakers → static
        degraded message. Provider and breaker failures are non-fatal: the error
        is recorded and the next provider is tried.

        Cost-aware routing (when ``budget_usd`` is set): at/above 100% of budget
        the gateway will not make a paid call at all — it serves cache or the
        static fallback (``route="budget_exhausted"``); between 80% and 100% it
        tries the cheapest provider first.
        """
        # 1. CACHE CHECK
        if self.cache is not None:
            cached_text, score = self.cache.get(prompt)
            if cached_text is not None:
                return GatewayResponse(
                    text=cached_text,
                    route=f"cache_hit:{score:.2f}",
                    provider=None,
                    cache_hit=True,
                    latency_ms=0.0,
                    estimated_cost=0.0,
                )

        # 1b. HARD COST CAP — cache missed and we are out of budget.
        if self.budget_usd is not None and self.cumulative_cost >= self.budget_usd:
            return GatewayResponse(
                text=_STATIC_DEGRADED,
                route="budget_exhausted",
                provider=None,
                cache_hit=False,
                latency_ms=0.0,
                estimated_cost=0.0,
                error=f"cost budget ${self.budget_usd:.4f} exhausted "
                f"(spent ${self.cumulative_cost:.4f})",
            )

        # 2. PROVIDER FALLBACK CHAIN
        providers, mode = self._provider_order()
        last_error: str | None = None
        for index, provider in enumerate(providers):
            breaker = self.breakers[provider.name]
            try:
                response = breaker.call(provider.complete, prompt)
            except (ProviderError, CircuitOpenError) as exc:
                last_error = f"{provider.name}: {exc}"
                continue

            self.cumulative_cost += response.estimated_cost
            if self.cache is not None:
                self.cache.set(prompt, response.text, {"provider": provider.name})
            if mode == "cost_saver":
                route = "cost_saver_primary" if index == 0 else "cost_saver_fallback"
            else:
                route = "primary" if index == 0 else "fallback"
            return GatewayResponse(
                text=response.text,
                route=route,
                provider=provider.name,
                cache_hit=False,
                latency_ms=response.latency_ms,
                estimated_cost=response.estimated_cost,
            )

        # 3. STATIC FALLBACK
        return GatewayResponse(
            text=_STATIC_DEGRADED,
            route="static_fallback",
            provider=None,
            cache_hit=False,
            latency_ms=0.0,
            estimated_cost=0.0,
            error=last_error,
        )
