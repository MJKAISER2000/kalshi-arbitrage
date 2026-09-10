"""Rate limiter tests.

Kalshi's 429 carries no ``Retry-After`` and no ``X-RateLimit-*`` headers, so the only sound
strategy is to stay under the budget locally. These tests pin the budget arithmetic and the
separation between the Read and Write buckets.
"""

import asyncio

import pytest

from kalshi_arb.api.ratelimit import (
    DEFAULT_TOKEN_COST,
    TIER_BUDGETS,
    Bucket,
    RateLimiter,
    Tier,
)


def test_all_tiers_have_budgets():
    assert set(TIER_BUDGETS) == set(Tier)
    for read, write in TIER_BUDGETS.values():
        assert read > 0 and write > 0


def test_budgets_increase_with_tier():
    tiers = list(Tier)
    reads = [TIER_BUDGETS[t][0] for t in tiers]
    assert reads == sorted(reads)


def test_basic_tier_sustained_request_rate():
    """Basic is 200 read tokens/sec at 10 tokens each -- about 20 reads/sec.

    This is why a REST-polling scanner cannot cover the whole exchange at Basic tier.
    """
    read, write = TIER_BUDGETS[Tier.BASIC]
    assert read / DEFAULT_TOKEN_COST == 20
    assert write / DEFAULT_TOKEN_COST == 10


def test_buckets_start_full():
    limiter = RateLimiter(Tier.PREMIER, now=0.0)
    assert limiter.available(Bucket.READ, now=0.0) == limiter.capacity(Bucket.READ)


def test_read_and_write_budgets_are_independent():
    """Draining reads must never delay an order or a cancel."""
    limiter = RateLimiter(Tier.BASIC, now=0.0)
    asyncio.run(limiter.acquire(Bucket.READ, 200))
    assert limiter.available(Bucket.WRITE, now=0.0) == limiter.capacity(Bucket.WRITE)


def test_above_basic_allows_a_two_second_burst():
    """Idle time banks tokens, so an event-driven client can burst on a move."""
    limiter = RateLimiter(Tier.PREMIER, now=0.0)
    read_rate = TIER_BUDGETS[Tier.PREMIER][0]
    assert limiter.capacity(Bucket.READ) == read_rate * 2


def test_basic_tier_banks_only_one_second():
    limiter = RateLimiter(Tier.BASIC, now=0.0)
    assert limiter.capacity(Bucket.READ) == TIER_BUDGETS[Tier.BASIC][0]


def test_wait_time_is_zero_while_tokens_remain():
    limiter = RateLimiter(Tier.BASIC, now=0.0)
    assert limiter.wait_time(Bucket.READ, 10, now=0.0) == 0.0


def test_wait_time_grows_once_drained():
    limiter = RateLimiter(Tier.BASIC, now=0.0)
    asyncio.run(limiter.acquire(Bucket.READ, 200))  # drain the bucket
    delay = limiter.wait_time(Bucket.READ, 100)
    assert delay > 0


def test_bucket_refills_over_time():
    limiter = RateLimiter(Tier.BASIC, now=0.0)
    bucket = limiter._buckets[Bucket.READ]
    bucket.tokens = 0.0
    bucket.updated = 0.0
    # 200 tokens/sec: half a second restores 100.
    assert bucket.wait_time(100, 0.5) == 0.0
    assert bucket.wait_time(150, 0.5) > 0


def test_refill_never_exceeds_capacity():
    limiter = RateLimiter(Tier.BASIC, now=0.0)
    assert limiter.available(Bucket.READ, now=10_000.0) == limiter.capacity(Bucket.READ)


def test_oversized_request_raises_instead_of_deadlocking():
    """A batch larger than the bucket can never be satisfied; waiting would hang forever.

    Batch endpoints cost per item, so a 25-order batch is 250 tokens and the whole batch is
    rejected unless it fits at once.
    """
    limiter = RateLimiter(Tier.BASIC, now=0.0)
    with pytest.raises(ValueError, match="can never fit"):
        limiter.wait_time(Bucket.WRITE, 5000)


def test_zero_or_negative_cost_rejected():
    limiter = RateLimiter(Tier.BASIC, now=0.0)
    with pytest.raises(ValueError, match="must be positive"):
        limiter.wait_time(Bucket.READ, 0)


def test_acquire_reports_time_spent_waiting():
    """Throttling must be observable, not hidden as unexplained latency."""

    async def scenario() -> float:
        limiter = RateLimiter(Tier.PRESTIGE)
        await limiter.acquire(Bucket.READ, int(limiter.capacity(Bucket.READ)))
        return await limiter.acquire(Bucket.READ, 100)

    assert asyncio.run(scenario()) > 0


def test_concurrent_acquirers_do_not_overdraw():
    """Without the per-bucket lock, racing coroutines would each see the same tokens."""

    async def scenario() -> float:
        limiter = RateLimiter(Tier.BASIC)
        await asyncio.gather(*(limiter.acquire(Bucket.READ, 10) for _ in range(20)))
        return limiter.available(Bucket.READ)

    remaining = asyncio.run(scenario())
    assert remaining < 10  # 20 x 10 tokens consumed from a 200-token bucket


def test_tier_accepts_a_plain_string():
    assert RateLimiter("basic").tier is Tier.BASIC
