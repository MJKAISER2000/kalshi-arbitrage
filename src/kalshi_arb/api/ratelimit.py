"""Client-side rate limiting.

Kalshi meters requests with two independent token buckets -- **Read** (GETs) and **Write**
(order placement, amend, cancel, order groups, the RFQ quote flow). Most requests cost 10
tokens. Buckets refill continuously at the tier's per-second budget; above the Basic tier they
hold two seconds of budget, allowing a 2x burst.

We enforce the budget locally rather than discovering it through 429s, because a 429 carries
**no ``Retry-After`` and no ``X-RateLimit-*`` headers** -- there is nothing to back off
against except guesswork. Self-limiting also keeps a scanner from starving the order path:
the two buckets are separate here exactly as they are server-side, so a burst of market-data
reads can never delay a cancel.

Batch endpoints do **not** save tokens: a 25-order batch costs 25x the per-order cost and is
rejected outright unless the whole amount is available at once.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import StrEnum

__all__ = ["DEFAULT_TOKEN_COST", "TIER_BUDGETS", "Bucket", "RateLimiter", "Tier"]

DEFAULT_TOKEN_COST = 10
"""Cost of most endpoints. GET /account/endpoint_costs is authoritative for the exceptions."""


class Bucket(StrEnum):
    READ = "read"
    WRITE = "write"


class Tier(StrEnum):
    BASIC = "basic"
    ADVANCED = "advanced"
    EXPERT = "expert"
    PREMIER = "premier"
    PARAGON = "paragon"
    PRIME = "prime"
    PRESTIGE = "prestige"


# Per-second token budgets, (read, write), from the published rate-limit table.
TIER_BUDGETS: dict[Tier, tuple[int, int]] = {
    Tier.BASIC: (200, 100),
    Tier.ADVANCED: (300, 300),
    Tier.EXPERT: (600, 600),
    Tier.PREMIER: (1000, 1000),
    Tier.PARAGON: (2000, 2000),
    Tier.PRIME: (4000, 4000),
    Tier.PRESTIGE: (10000, 8000),
}


@dataclass
class _TokenBucket:
    """A continuously refilling token bucket."""

    rate: float
    """Tokens added per second."""
    capacity: float
    """Maximum tokens held, which sets the burst size."""
    tokens: float
    updated: float

    def _refill(self, now: float) -> None:
        elapsed = now - self.updated
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self.updated = now

    def wait_time(self, cost: float, now: float) -> float:
        """Seconds until ``cost`` tokens are available. Zero if available now."""
        self._refill(now)
        if self.tokens >= cost:
            return 0.0
        return (cost - self.tokens) / self.rate

    def consume(self, cost: float, now: float) -> None:
        self._refill(now)
        self.tokens -= cost


class RateLimiter:
    """Enforces the Read and Write budgets for one API tier.

    A request that costs more than the bucket's whole capacity can never be satisfied and
    raises immediately rather than deadlocking -- which is what a too-large order batch would
    otherwise do.
    """

    def __init__(self, tier: Tier | str = Tier.BASIC, *, now: float | None = None) -> None:
        self.tier = Tier(tier)
        read_rate, write_rate = TIER_BUDGETS[self.tier]
        # Basic-tier write buckets, and Read buckets above Advanced, hold one second of
        # budget; the rest hold two. One second is the safe assumption when unsure, since
        # under-bursting costs latency while over-bursting costs 429s.
        burst = 1.0 if self.tier is Tier.BASIC else 2.0
        start = time.monotonic() if now is None else now
        self._buckets = {
            Bucket.READ: _TokenBucket(read_rate, read_rate * burst, read_rate * burst, start),
            Bucket.WRITE: _TokenBucket(write_rate, write_rate * burst, write_rate * burst, start),
        }
        self._locks = {b: asyncio.Lock() for b in Bucket}

    def capacity(self, bucket: Bucket) -> float:
        return self._buckets[bucket].capacity

    def available(self, bucket: Bucket, *, now: float | None = None) -> float:
        b = self._buckets[bucket]
        b._refill(time.monotonic() if now is None else now)
        return b.tokens

    def wait_time(
        self, bucket: Bucket, cost: int = DEFAULT_TOKEN_COST, *, now: float | None = None
    ) -> float:
        """How long a request of ``cost`` would have to wait right now."""
        self._check_cost(bucket, cost)
        return self._buckets[bucket].wait_time(cost, time.monotonic() if now is None else now)

    def _check_cost(self, bucket: Bucket, cost: int) -> None:
        if cost <= 0:
            raise ValueError(f"token cost must be positive (got {cost})")
        capacity = self._buckets[bucket].capacity
        if cost > capacity:
            raise ValueError(
                f"a {cost}-token request can never fit the {self.tier.value} {bucket.value} "
                f"bucket (capacity {capacity:.0f}). Split the batch or raise the tier."
            )

    async def acquire(self, bucket: Bucket, cost: int = DEFAULT_TOKEN_COST) -> float:
        """Wait until ``cost`` tokens are available, then consume them.

        Returns how long it waited, so callers can log throttling rather than have it hide as
        unexplained latency. The per-bucket lock keeps concurrent callers from each seeing the
        same tokens and collectively overdrawing.
        """
        self._check_cost(bucket, cost)
        async with self._locks[bucket]:
            waited = 0.0
            while True:
                delay = self._buckets[bucket].wait_time(cost, time.monotonic())
                if delay <= 0:
                    self._buckets[bucket].consume(cost, time.monotonic())
                    return waited
                await asyncio.sleep(delay)
                waited += delay
