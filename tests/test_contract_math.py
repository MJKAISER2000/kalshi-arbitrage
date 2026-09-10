"""Binary contract mathematics tests.

Includes the invariants that must hold for the platform's arbitrage claims to mean anything,
and the negative results established during API research.
"""

from decimal import Decimal

import pytest

from kalshi_arb.contracts import (
    Exclusivity,
    Outcome,
    PayoffBounds,
    basket_payoff_bounds,
    complement_invariant_holds,
    complement_price,
    derive_ask,
    effective_spread,
    guaranteed_gross_edge,
    implied_probability,
    position_payoff,
)
from kalshi_arb.types import MoneyError

D = Decimal


# ---------------------------------------------------------------------------
# Complements and payoffs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("price", ["0", "0.01", "0.4237", "0.50", "0.99", "1"])
def test_complement_is_an_involution(price):
    assert complement_price(complement_price(price)) == D(price)


@pytest.mark.parametrize("resolved_yes", [True, False])
def test_yes_and_no_payoffs_sum_to_notional(resolved_yes):
    """The defining property of a binary contract: exactly one side pays."""
    y = position_payoff(Outcome.YES, resolved_yes, 7)
    n = position_payoff(Outcome.NO, resolved_yes, 7)
    assert y + n == D(7)
    assert (y == 0) != (n == 0)


def test_payoff_respects_non_unit_notional():
    assert position_payoff(Outcome.YES, True, 3, notional=D(2)) == D(6)


def test_opposite_side():
    assert Outcome.YES.opposite is Outcome.NO
    assert Outcome.NO.opposite.opposite is Outcome.NO


def test_implied_probability_is_price_over_notional():
    assert implied_probability("0.42") == D("0.42")
    assert implied_probability("1.00", notional=D(2)) == D("0.5")


def test_fractional_contract_payoff():
    assert position_payoff(Outcome.YES, True, "2.50") == D("2.50")


# ---------------------------------------------------------------------------
# Derived asks and the retired single-market "arbitrage"
# ---------------------------------------------------------------------------


def test_ask_is_derived_from_opposite_bid():
    """Kalshi publishes bids only; a YES bid at $0.60 is a NO ask at $0.40."""
    assert derive_ask("0.60") == D("0.40")


def test_single_market_complement_arbitrage_is_exactly_negative_spread():
    """The core negative result.

    Buying both sides costs yes_ask + no_ask and pays notional, so the "profit" is always
    -(spread). It can never be positive without a crossed book.
    """
    for yes_bid, no_bid in [("0.42", "0.56"), ("0.10", "0.85"), ("0.50", "0.49")]:
        yes_ask = derive_ask(no_bid)
        no_ask = derive_ask(yes_bid)
        profit = D(1) - (yes_ask + no_ask)
        assert profit == -effective_spread(yes_bid, no_bid)
        assert profit <= 0


def test_complement_invariant_holds_for_uncrossed_books():
    assert complement_invariant_holds("0.42", "0.56")
    assert complement_invariant_holds("0.50", "0.50")  # touching, zero spread


def test_complement_invariant_detects_crossed_book():
    """A violation is a data-quality alarm, not an opportunity."""
    assert not complement_invariant_holds("0.60", "0.45")


def test_effective_spread_matches_ask_minus_bid():
    yes_bid, no_bid = "0.42", "0.56"
    assert effective_spread(yes_bid, no_bid) == derive_ask(no_bid) - D(yes_bid) == D("0.02")


# ---------------------------------------------------------------------------
# Basket payoff bounds -- the payoff-state proof
# ---------------------------------------------------------------------------


def test_buying_all_no_is_safe_under_mutual_exclusivity():
    """At most one YES means at least N-1 NO legs pay. No exhaustiveness needed."""
    b = basket_payoff_bounds(8, Outcome.NO, Exclusivity.MUTUALLY_EXCLUSIVE)
    assert b.minimum == D(7)
    assert b.maximum == D(8)  # if no outcome occurs, all 8 pay


def test_buying_all_yes_guarantees_nothing_under_mutual_exclusivity():
    """The headline false-arbitrage trap: zero outcomes may occur, losing all premium."""
    b = basket_payoff_bounds(8, Outcome.YES, Exclusivity.MUTUALLY_EXCLUSIVE)
    assert b.minimum == D(0)
    assert b.maximum == D(1)
    assert not b.is_certain


def test_partition_makes_all_yes_certain():
    """With proven exhaustiveness, exactly one YES pays -- now it is a real arbitrage leg."""
    b = basket_payoff_bounds(8, Outcome.YES, Exclusivity.PARTITION)
    assert b.is_certain and b.minimum == D(1)
    n = basket_payoff_bounds(8, Outcome.NO, Exclusivity.PARTITION)
    assert n.is_certain and n.minimum == D(7)


def test_unconstrained_event_guarantees_nothing():
    """164 of 200 live events are not mutually exclusive."""
    b = basket_payoff_bounds(5, Outcome.NO, Exclusivity.UNCONSTRAINED)
    assert b.minimum == D(0)
    assert b.maximum == D(5)


def test_basket_bounds_scale_with_count_and_notional():
    b = basket_payoff_bounds(4, Outcome.NO, Exclusivity.MUTUALLY_EXCLUSIVE, count=10)
    assert (b.minimum, b.maximum) == (D(30), D(40))
    b2 = basket_payoff_bounds(
        4, Outcome.NO, Exclusivity.MUTUALLY_EXCLUSIVE, count=10, notional=D(2)
    )
    assert (b2.minimum, b2.maximum) == (D(60), D(80))


def test_yes_and_no_basket_payoffs_are_complementary():
    """Across any exclusivity, the two baskets together pay N * notional in every state."""
    for excl in Exclusivity:
        y = basket_payoff_bounds(6, Outcome.YES, excl)
        n = basket_payoff_bounds(6, Outcome.NO, excl)
        assert y.minimum + n.maximum == D(6)
        assert y.maximum + n.minimum == D(6)


def test_single_leg_basket_reduces_to_a_binary_contract():
    b = basket_payoff_bounds(1, Outcome.NO, Exclusivity.PARTITION)
    assert b.is_certain and b.minimum == D(0)  # the one leg must resolve YES


def test_empty_basket_rejected():
    with pytest.raises(MoneyError, match="at least one leg"):
        basket_payoff_bounds(0, Outcome.NO, Exclusivity.MUTUALLY_EXCLUSIVE)


# ---------------------------------------------------------------------------
# Guaranteed edge uses the worst state
# ---------------------------------------------------------------------------


def test_guaranteed_edge_uses_minimum_payoff():
    """An arbitrage claim must survive the worst permitted settlement state."""
    bounds = PayoffBounds(minimum=D(7), maximum=D(8))
    assert guaranteed_gross_edge("6.95", bounds) == D("0.05")


def test_guaranteed_edge_negative_when_cost_exceeds_worst_case():
    bounds = basket_payoff_bounds(8, Outcome.YES, Exclusivity.MUTUALLY_EXCLUSIVE)
    # Buying all YES for $0.95 looks like a 5c edge -- but the worst case pays nothing.
    assert guaranteed_gross_edge("0.95", bounds) == D("-0.95")


def test_payoff_bounds_reject_inverted_range():
    with pytest.raises(MoneyError, match="exceeds maximum"):
        PayoffBounds(minimum=D(5), maximum=D(1))


# ---------------------------------------------------------------------------
# Input hygiene
# ---------------------------------------------------------------------------


def test_float_prices_rejected():
    with pytest.raises(MoneyError, match="float is not accepted"):
        complement_price(0.42)


@pytest.mark.parametrize("bad", ["-0.01", "1.5"])
def test_prices_outside_notional_rejected(bad):
    with pytest.raises(MoneyError, match="outside"):
        complement_price(bad)


def test_four_decimal_prices_preserved_exactly():
    """Sub-cent ticks are real; precision must not be lost."""
    assert complement_price("0.0001") == D("0.9999")
    assert implied_probability("0.4237") == D("0.4237")
