"""Property-based tests for mathematical invariants (brief section 40).

These assert properties that must hold for *every* input, not just chosen examples. They are
the guardrail against a plausible-looking refactor quietly breaking payoff or fee arithmetic.

Prices are generated as integer tick counts and scaled with ``Decimal`` so no float ever
enters the test either.
"""

from decimal import Decimal

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from kalshi_arb.contracts import (
    Exclusivity,
    Outcome,
    basket_payoff_bounds,
    complement_price,
    effective_spread,
    implied_probability,
    position_payoff,
)
from kalshi_arb.fees import FeeModel, FeeSchedule, MemberPrecision, Role

D = Decimal
MODEL = FeeModel()

# Prices on the finest documented grid: 0.0000 .. 1.0000 in $0.0001 steps.
TICKS = st.integers(min_value=0, max_value=10_000)
COUNTS = st.integers(min_value=1, max_value=100_000)
LEGS = st.integers(min_value=1, max_value=50)


def price(tick: int) -> Decimal:
    return (D(tick) / D(10_000)).quantize(D("0.0001"))


# ---------------------------------------------------------------------------
# Contract invariants
# ---------------------------------------------------------------------------


@given(TICKS)
def test_complement_round_trips(tick):
    p = price(tick)
    assert complement_price(complement_price(p)) == p


@given(TICKS)
def test_yes_and_no_prices_sum_to_notional(tick):
    p = price(tick)
    assert p + complement_price(p) == D(1)


@given(TICKS, st.booleans(), COUNTS)
def test_binary_payoff_consistency(tick, resolved_yes, count):
    """YES payoff + NO payoff == notional * count, in every resolution state."""
    y = position_payoff(Outcome.YES, resolved_yes, count)
    n = position_payoff(Outcome.NO, resolved_yes, count)
    assert y + n == D(count)


@given(TICKS)
def test_implied_probability_is_a_probability(tick):
    assert D(0) <= implied_probability(price(tick)) <= D(1)


@given(TICKS, TICKS)
def test_single_market_complement_profit_is_never_positive(yes_tick, no_tick):
    """The retired Strategy #1, as a universal property.

    For any *uncrossed* book, buying both sides can never profit -- the "edge" is exactly the
    negative spread. A positive value would mean the matching engine failed.
    """
    yes_bid, no_bid = price(yes_tick), price(no_tick)
    # A crossed book is not a state the matching engine permits.
    assume(yes_bid + no_bid <= D(1))
    yes_ask = D(1) - no_bid
    no_ask = D(1) - yes_bid
    profit = D(1) - (yes_ask + no_ask)
    assert profit <= 0
    assert profit == -effective_spread(yes_bid, no_bid)


# ---------------------------------------------------------------------------
# Basket invariants
# ---------------------------------------------------------------------------


@given(LEGS, st.sampled_from(list(Exclusivity)), st.sampled_from(list(Outcome)), COUNTS)
def test_basket_bounds_are_ordered_and_non_negative(n, excl, side, count):
    b = basket_payoff_bounds(n, side, excl, count=count)
    assert D(0) <= b.minimum <= b.maximum <= D(n) * count


@given(LEGS, st.sampled_from(list(Exclusivity)), COUNTS)
def test_yes_and_no_baskets_are_complementary(n, excl, count):
    """Owning every YES leg and every NO leg pays N * notional in every state."""
    y = basket_payoff_bounds(n, Outcome.YES, excl, count=count)
    no = basket_payoff_bounds(n, Outcome.NO, excl, count=count)
    total = D(n) * count
    assert y.minimum + no.maximum == total
    assert y.maximum + no.minimum == total


@given(LEGS, COUNTS)
def test_mutual_exclusivity_guarantees_all_no_basket(n, count):
    """At most one YES => at least (N-1) NO legs pay, always."""
    b = basket_payoff_bounds(n, Outcome.NO, Exclusivity.MUTUALLY_EXCLUSIVE, count=count)
    assert b.minimum == D(n - 1) * count


@given(LEGS, COUNTS)
def test_mutual_exclusivity_guarantees_nothing_for_all_yes_basket(n, count):
    """Without exhaustiveness the worst case is a total loss of premium."""
    b = basket_payoff_bounds(n, Outcome.YES, Exclusivity.MUTUALLY_EXCLUSIVE, count=count)
    assert b.minimum == D(0)


@given(LEGS, st.sampled_from(list(Outcome)), COUNTS)
def test_partition_payoff_is_always_certain(n, side, count):
    """A proven partition removes settlement uncertainty entirely."""
    assert basket_payoff_bounds(n, side, Exclusivity.PARTITION, count=count).is_certain


# ---------------------------------------------------------------------------
# Fee invariants
# ---------------------------------------------------------------------------


@given(TICKS, COUNTS)
@settings(max_examples=200)
def test_fee_is_non_negative_and_rounds_up(tick, count):
    b = MODEL.calculate_fee(count, price(tick))
    assert b.fee >= 0
    assert b.fee >= b.raw
    assert b.rounding_cost >= 0


@given(TICKS, COUNTS)
@settings(max_examples=200)
def test_fee_rounding_never_exceeds_one_unit_of_precision(tick, count):
    """Round-up cost is bounded by the member's balance precision."""
    b = MODEL.calculate_fee(count, price(tick))
    assert b.rounding_cost < MemberPrecision.NON_DIRECT.value


@given(TICKS)
def test_fee_is_symmetric_about_the_midpoint(tick):
    """p*(1-p) is symmetric, so a contract and its complement cost the same to trade."""
    p = price(tick)
    sched = FeeSchedule()
    assert MODEL.raw_fee(100, p, schedule=sched) == MODEL.raw_fee(
        100, complement_price(p), schedule=sched
    )


@given(TICKS, COUNTS, COUNTS)
@settings(max_examples=200)
def test_fee_is_monotone_in_size(tick, a, b):
    """More contracts never costs less."""
    lo, hi = sorted((a, b))
    sched = FeeSchedule()
    assert MODEL.raw_fee(lo, price(tick), schedule=sched) <= MODEL.raw_fee(
        hi, price(tick), schedule=sched
    )


@given(TICKS, COUNTS)
@settings(max_examples=200)
def test_taker_effective_price_is_never_better_than_the_quote(tick, count):
    """Crossing the spread always costs something."""
    p = price(tick)
    assert MODEL.effective_price(count, p) >= p


@given(TICKS, COUNTS)
@settings(max_examples=200)
def test_maker_fee_never_exceeds_taker_fee(tick, count):
    sched = FeeSchedule()
    maker = MODEL.calculate_fee(count, price(tick), schedule=sched, role=Role.MAKER).fee
    taker = MODEL.calculate_fee(count, price(tick), schedule=sched, role=Role.TAKER).fee
    assert maker <= taker


@given(st.lists(st.tuples(COUNTS, TICKS), min_size=1, max_size=20))
@settings(max_examples=100)
def test_basket_fee_is_at_least_the_sum_of_raw_leg_fees(legs):
    """Per-order rounding means a basket can only cost more than the un-rounded total.

    This is the property that makes leg count a real cost driver.
    """
    pairs = [(c, price(t)) for c, t in legs]
    total = MODEL.calculate_basket_cost(pairs)
    raw = sum((MODEL.raw_fee(c, p, schedule=FeeSchedule()) for c, p in pairs), D(0))
    assert total >= raw


@given(st.lists(st.tuples(COUNTS, TICKS), min_size=1, max_size=20))
@settings(max_examples=100)
def test_basket_fee_equals_sum_of_independently_rounded_legs(legs):
    pairs = [(c, price(t)) for c, t in legs]
    expected = sum((MODEL.calculate_fee(c, p).fee for c, p in pairs), D(0))
    assert MODEL.calculate_basket_cost(pairs) == expected
