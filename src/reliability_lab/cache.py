from __future__ import annotations

import hashlib
import math
import re
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Shared utilities — use these in both ResponseCache and SharedRedisCache
# ---------------------------------------------------------------------------

PRIVACY_PATTERNS = re.compile(
    r"\b(balance|password|credit.card|ssn|social.security|user.\d+|account.\d+)\b",
    re.IGNORECASE,
)


def _is_uncacheable(query: str) -> bool:
    """Return True if query contains privacy-sensitive keywords."""
    return bool(PRIVACY_PATTERNS.search(query))


def _looks_like_false_hit(query: str, cached_key: str) -> bool:
    """Return True if query and cached key contain different 4-digit numbers (years, IDs)."""
    nums_q = set(re.findall(r"\b\d{4}\b", query))
    nums_c = set(re.findall(r"\b\d{4}\b", cached_key))
    return bool(nums_q and nums_c and nums_q != nums_c)


# ---------------------------------------------------------------------------
# In-memory cache (existing)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CacheEntry:
    key: str
    value: str
    created_at: float
    metadata: dict[str, str]


class ResponseCache:
    """Simple in-memory cache skeleton.

    TODO(student): Add a better semantic similarity function and false-hit guardrails.
    Use the module-level _is_uncacheable() and _looks_like_false_hit() helpers in your
    get() and set() methods.  For production, replace with SharedRedisCache.
    """

    def __init__(self, ttl_seconds: int, similarity_threshold: float):
        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self._entries: list[CacheEntry] = []
        self.false_hit_log: list[dict[str, object]] = []

    def get(self, query: str) -> tuple[str | None, float]:
        """Look up a cached response by semantic similarity, with guardrails.

        Returns ``(value, score)`` on a safe hit, otherwise ``(None, score)`` where
        ``score`` is the best similarity seen (0.0 when nothing matched).
        """
        # 1. Privacy guardrail — never serve sensitive queries from cache.
        if _is_uncacheable(query):
            return None, 0.0

        # 2. Evict expired entries lazily.
        now = time.time()
        self._entries = [e for e in self._entries if now - e.created_at <= self.ttl_seconds]

        # 3. Find the closest cached entry.
        best_score = 0.0
        best_value: str | None = None
        best_key: str | None = None
        for entry in self._entries:
            score = self.similarity(query, entry.key)
            if score > best_score:
                best_score = score
                best_value = entry.value
                best_key = entry.key

        # 4. Only accept matches at or above the configured threshold.
        if best_value is not None and best_score >= self.similarity_threshold and best_key is not None:
            # 4a. False-hit guardrail — different years/IDs mean different intent.
            if _looks_like_false_hit(query, best_key):
                self.false_hit_log.append(
                    {
                        "query": query,
                        "cached_key": best_key,
                        "score": round(best_score, 4),
                        "reason": "date_or_number_mismatch",
                    }
                )
                return None, best_score
            return best_value, best_score

        # 5. Nothing good enough.
        return None, best_score

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Store a response, unless the query is privacy-sensitive."""
        if _is_uncacheable(query):
            return
        self._entries.append(
            CacheEntry(
                key=query,
                value=value,
                created_at=time.time(),
                metadata=metadata or {},
            )
        )

    @staticmethod
    def similarity(a: str, b: str) -> float:
        """Cosine similarity over word tokens + character 3-grams.

        Bag-of-tokens cosine keeps far more signal than plain token-overlap
        (Jaccard): near-identical phrases that differ by one word still score
        high, while unrelated strings collapse toward zero.
        """
        if a == b:
            return 1.0

        def tokenize(text: str) -> Counter[str]:
            tokens: list[str] = []
            for word in text.lower().split():
                tokens.append(word)
                for i in range(len(word) - 2):
                    tokens.append(word[i : i + 3])
            return Counter(tokens)

        va, vb = tokenize(a), tokenize(b)
        if not va or not vb:
            return 0.0

        dot = sum(count * vb.get(token, 0) for token, count in va.items())
        norm_a = math.sqrt(sum(count * count for count in va.values()))
        norm_b = math.sqrt(sum(count * count for count in vb.values()))
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
        return dot / (norm_a * norm_b)


# ---------------------------------------------------------------------------
# Redis shared cache (new)
# ---------------------------------------------------------------------------


class SharedRedisCache:
    """Redis-backed shared cache for multi-instance deployments.

    TODO(student): Implement the get() and set() methods using Redis commands
    so that cache state is shared across multiple gateway instances.

    Data model (suggested):
        Key    = "{prefix}{query_hash}"   (Redis String namespace)
        Value  = Redis Hash with fields:  "query", "response"
        TTL    = Redis EXPIRE (automatic cleanup — no manual eviction)

    For similarity lookup: SCAN all keys with self.prefix, HGET each entry's
    "query" field, compute similarity locally via ResponseCache.similarity().

    Provided helpers:
        _is_uncacheable(query)          — True if privacy-sensitive
        _looks_like_false_hit(q, key)   — True if 4-digit numbers differ
        self._query_hash(query)         — deterministic short hash for Redis key
        ResponseCache.similarity(a, b)  — reuse your improved similarity function
    """

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int,
        similarity_threshold: float,
        prefix: str = "rl:cache:",
    ):
        import redis as redis_lib
        from redis.backoff import ExponentialBackoff
        from redis.retry import Retry

        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self.prefix = prefix
        self.false_hit_log: list[dict[str, object]] = []
        # Bounded timeouts + transparent retry on transient connect/timeout errors.
        # (Docker Desktop's port-forward can be slow to warm up on the first
        #  connection; a single retry hides that without masking a real outage.)
        self._redis: Any = redis_lib.Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=10,
            socket_timeout=10,
            retry=Retry(ExponentialBackoff(cap=1.0, base=0.2), retries=3),
            retry_on_timeout=True,
            retry_on_error=[redis_lib.exceptions.ConnectionError, redis_lib.exceptions.TimeoutError],
        )

    def ping(self) -> bool:
        """Check Redis connectivity."""
        try:
            return bool(self._redis.ping())
        except Exception:  # noqa: BLE001 - any failure here means "not reachable"
            return False

    def get(self, query: str) -> tuple[str | None, float]:
        """Look up a cached response from Redis.

        Redis EXPIRE handles eviction, so there is no TTL bookkeeping here.
        """
        if _is_uncacheable(query):
            return None, 0.0

        # 2-3. Exact match via the deterministic key.
        exact_key = f"{self.prefix}{self._query_hash(query)}"
        exact = self._redis.hget(exact_key, "response")
        if exact is not None:
            return exact, 1.0

        # 4-6. Similarity scan across all entries under this prefix.
        best_score = 0.0
        best_value: str | None = None
        best_query: str | None = None
        for key in self._redis.scan_iter(f"{self.prefix}*"):
            cached_query = self._redis.hget(key, "query")
            if not cached_query:
                continue
            score = ResponseCache.similarity(query, cached_query)
            if score > best_score:
                best_score = score
                best_value = self._redis.hget(key, "response")
                best_query = cached_query

        if best_value is not None and best_score >= self.similarity_threshold and best_query is not None:
            # 7. False-hit guardrail.
            if _looks_like_false_hit(query, best_query):
                self.false_hit_log.append(
                    {
                        "query": query,
                        "cached_key": best_query,
                        "score": round(best_score, 4),
                        "reason": "date_or_number_mismatch",
                    }
                )
                return None, best_score
            return best_value, best_score

        return None, best_score

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Store a response in Redis with TTL.

        Eviction is delegated to Redis via EXPIRE.
        """
        if _is_uncacheable(query):
            return
        key = f"{self.prefix}{self._query_hash(query)}"
        self._redis.hset(key, mapping={"query": query, "response": value})
        self._redis.expire(key, self.ttl_seconds)

    def flush(self) -> None:
        """Remove all entries with this cache prefix (for testing)."""
        for key in self._redis.scan_iter(f"{self.prefix}*"):
            self._redis.delete(key)

    def close(self) -> None:
        """Close Redis connection."""
        if self._redis is not None:
            self._redis.close()

    @staticmethod
    def _query_hash(query: str) -> str:
        """Deterministic short hash for a query string."""
        return hashlib.md5(query.lower().strip().encode()).hexdigest()[:12]
