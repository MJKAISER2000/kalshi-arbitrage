"""Fee model tests.

The headline cases are the **published General Trading Fees Table** from the official Kalshi
Fee Schedule (effective 2026-07-07). If the exchange changes its formula, these fail loudly
rather than silently repricing every strategy.
"""

from decimal import Decimal

import pytest

from kalshi_arb.fees import (
    FeeModel,
    FeeSchedule,
    FeeType,
    MemberPrecision,
    Role,
    UnmodelledFeeError,
)
from kalshi_arb.types import MoneyError

D = Decimal


@pytest.fixture
def model() -> FeeModel:
    return FeeModel()


# ---------------------------------------------------------------------------
# Conformance with the officially published fee table
# ---------------------------------------------------------------------------

# (price, fee for 1 contract, fee for 100 contracts) -- General Trading Fees Table,
# transcribed from the official schedule. These are cent-rounded, i.e. non-direct precision.
PUBLISHED_TABLE = [
    ("0.01", "0.01", "0.07"),
    ("0.05", "0.01", "0.34"),
    ("0.10", "0.01", "0.63"),
    ("0.15", "0.01", "0.90"),
    ("0.20", "0.02", "1.12"),
    ("0.25", "0.02", "1.32"),
    ("0.30", "0.02", "1.47"),
    ("0.35", "0.02", "1.60"),
    ("0.40", "0.02", "1.68"),
    ("0.45", "0.02", "1.74"),
    ("0.50", "0.02", "1.75"),
    ("0.55", "0.02", "1.74"),
    ("0.60", "0.02", "1.68"),
    ("0.65", "0.02", "1.60"),
    ("0.70", "0.02", "1.47"),
    ("0.75", "0.02", "1.32"),
    ("0.80", "0.02", "1.12"),
    ("0.85", "0.01", "0.90"),
    ("0.90", "0.01", "0.63"),
    ("0.95", "0.01", "0.34"),
    ("0.99", "0.01", "0.07"),
]


@pytest.mark.parametrize("price,fee_1,fee_100", PUBLISHED_TABLE)
def test_matches_published_fee_table(model, price, fee_1, fee_100):
    """Every row of the official table, both order sizes."""
    assert model.calculate_fee(1, price).fee == D(fee_1)
    assert model.calculate_fee(100, price).fee == D(fee_100)


def test_round_up_applies_to_order_total_not_per_contract(model):
    """100 @ $0.35 -> $1.5925 rounds to $1.60, not 100 * ceil($0.015925) = $2.00.

    Pinning this matters: per-contract rounding would overstate fees ~25% here and change
    which baskets pass the edge filter.
    """
    assert model.raw_fee(100, "0.35", schedule=FeeSchedule()) == D("1.5925")
    assert model.calculate_fee(100, "0.35").fee == D("1.60")


def test_fee_peaks_at_midpoint(model):
    """Quadratic in price: most expensive at $0.50, symmetric about it."""
    mid = model.raw_fee(100, "0.50", schedule=FeeSchedule())
    assert mid == D("1.75")
    for lo, hi in [("0.40", "0.60"), ("0.25", "0.75"), ("0.01", "0.99")]:
        assert model.raw_fee(100, lo, schedule=FeeSchedule()) < mid
        assert model.raw_fee(100, lo, schedule=FeeSchedule()) == model.raw_fee(
            100, hi, schedule=FeeSchedule()
        )


# ---------------------------------------------------------------------------
# Structure: multiplier, roles, precision
# ---------------------------------------------------------------------------


def test_series_multiplier_scales_the_base_rate(model):
    """M is a multiplier on 0.07, not the rate itself. M=0.5 gives the old 0.035 rate."""
    half = FeeSchedule(taker_multiplier=D("0.5"))
    assert half.rate(Role.TAKER) == D("0.035")
    assert model.raw_fee(100, "0.50", schedule=half) == D("0.875")
    assert model.calculate_fee(100, "0.50", schedule=half).fee == D("0.88")


def test_zero_multiplier_series_pay_no_fee(model):
    """Some series carry M=0 on both sides, so the fee term vanishes entirely."""
    free = FeeSchedule(taker_multiplier=D(0), maker_multiplier=D(0))
    assert model.calculate_fee(100, "0.50", schedule=free).fee == D(0)


def test_from_api_treats_fee_multiplier_as_a_multiplier_not_a_rate(model):
    """Regression guard on a ~14x error.

    The API's fee_multiplier is M (default 1). Passing it in as the rate would compute
    1 * C * P * (1-P) instead of 1 * 0.07 * C * P * (1-P).
    """
    sched = FeeSchedule.from_api("quadratic", 1)
    assert sched.rate(Role.TAKER) == D("0.07")
    assert model.raw_fee(100, "0.50", schedule=sched) == D("1.75")
    assert model.raw_fee(100, "0.50", schedule=sched) != D("25")


def test_from_api_derives_maker_multiplier_from_fee_type(model):
    """Maker M comes from fee_type: 0 plain, 1 with maker fees, 2 for combos."""
    assert FeeSchedule.from_api("quadratic", 1).rate(Role.MAKER) == D(0)
    assert FeeSchedule.from_api("quadratic_with_maker_fees", 1).rate(Role.MAKER) == D("0.0175")
    assert FeeSchedule.from_api("quadratic_with_combo_maker_fees", 1).rate(Role.MAKER) == D("0.035")


def test_from_api_rejects_unknown_fee_type():
    with pytest.raises(UnmodelledFeeError, match="unknown fee_type"):
        FeeSchedule.from_api("some_new_structure", 1)


def test_plain_quadratic_charges_maker_nothing(model):
    """Resting orders that do not immediately match pay no trading fee."""
    sched = FeeSchedule(fee_type=FeeType.QUADRATIC)
    assert sched.effective_maker_multiplier() == D(0)
    assert sched.rate(Role.MAKER) == D(0)
    assert model.calculate_fee(100, "0.50", schedule=sched, role=Role.MAKER).fee == D(0)
    assert model.calculate_fee(100, "0.50", schedule=sched, role=Role.TAKER).fee == D("1.75")


def test_zero_fee_does_not_round_up_to_a_cent(model):
    """A free maker fill must report $0.00, not ceil(0) mishandled into $0.01."""
    sched = FeeSchedule(fee_type=FeeType.QUADRATIC)
    assert model.calculate_fee(1, "0.50", schedule=sched, role=Role.MAKER).fee == D(0)


@pytest.mark.parametrize(
    "fee_type,maker_m,fraction_of_taker",
    [
        (FeeType.QUADRATIC_WITH_MAKER_FEES, D(1), D("0.25")),
        (FeeType.QUADRATIC_WITH_COMBO_MAKER_FEES, D(2), D("0.5")),
    ],
)
def test_maker_rate_is_a_fraction_of_the_taker_rate(model, fee_type, maker_m, fraction_of_taker):
    """0.0175 = 0.25 * 0.07, so maker M of 1 and 2 give a quarter and a half."""
    sched = FeeSchedule(fee_type=fee_type)
    assert sched.effective_maker_multiplier() == maker_m
    assert sched.rate(Role.MAKER) == sched.rate(Role.TAKER) * fraction_of_taker
    assert model.raw_fee(100, "0.50", schedule=sched, role=Role.MAKER) == (
        D("1.75") * fraction_of_taker
    )


def test_direct_member_gets_finer_rounding(model):
    """$0.0001 precision costs less in round-up than $0.01."""
    fine = FeeSchedule(precision=MemberPrecision.DIRECT)
    coarse = FeeSchedule(precision=MemberPrecision.NON_DIRECT)
    assert model.calculate_fee(1, "0.35", schedule=fine).fee == D("0.0160")
    assert model.calculate_fee(1, "0.35", schedule=coarse).fee == D("0.02")


def test_flat_fee_type_refuses_to_guess(model):
    """Unmodelled schedule must raise, never silently price at zero or at the quadratic rate."""
    with pytest.raises(UnmodelledFeeError, match="flat"):
        model.raw_fee(10, "0.50", schedule=FeeSchedule(fee_type=FeeType.FLAT))


def test_flat_fee_works_when_verified_rate_supplied(model):
    sched = FeeSchedule(fee_type=FeeType.FLAT, flat_fee_per_contract=D("0.01"))
    assert model.raw_fee(50, "0.50", schedule=sched) == D("0.50")


# ---------------------------------------------------------------------------
# Basket behaviour -- the term that killed the live opportunity
# ---------------------------------------------------------------------------


def test_basket_rounds_each_leg_independently(model):
    """Per-order rounding. Rounding the summed raw fee once would understate the cost."""
    legs = [(1, "0.10")] * 8  # raw 0.0063 each -> 0.01 each
    assert model.calculate_basket_cost(legs) == D("0.08")
    summed_raw = sum(model.raw_fee(1, "0.10", schedule=FeeSchedule()) for _ in range(8))
    assert summed_raw == D("0.0504")  # would round to $0.06 -- understates by $0.02


def test_eight_leg_basket_fees_can_exceed_gross_edge(model):
    """The core reason multi-leg baskets are hard here, as a regression test.

    Eight mutually-exclusive legs whose YES bids sum to $1.05 carry a real $0.05 gross
    overround per basket. Buying all eight NO legs at 10 contracts guarantees at least seven
    payouts -- and still loses money, because each leg is a separate order with its own
    round-up while the edge does not scale with leg count.

    Prices are representative of a real multi-outcome event; the point is the arithmetic.
    """
    yes_bids = ["0.07", "0.12", "0.22", "0.15", "0.07", "0.08", "0.14", "0.20"]
    count = 10
    no_prices = [D(1) - D(b) for b in yes_bids]
    legs = [(count, p) for p in no_prices]

    cost = sum(p * count for p in no_prices)
    guaranteed_payoff = D(len(yes_bids) - 1) * count  # at most one YES -> >= N-1 legs pay
    gross = guaranteed_payoff - cost

    assert gross == D("0.50")  # the gross edge is real

    # The conclusion must not depend on which balance precision applies.
    coarse = model.calculate_basket_cost(
        legs, schedule=FeeSchedule(precision=MemberPrecision.NON_DIRECT)
    )
    fine = model.calculate_basket_cost(legs, schedule=FeeSchedule(precision=MemberPrecision.DIRECT))
    assert gross < fine < coarse  # fees exceed the edge under either precision


def test_effective_price_exceeds_quoted_price(model):
    """All-in cost per contract must be strictly worse than the quote for a taker."""
    assert model.effective_price(100, "0.50") == D("0.5175")
    assert model.effective_price(100, "0.50") > D("0.50")


def test_effective_price_rejects_zero_count(model):
    with pytest.raises(MoneyError, match="count must be positive"):
        model.effective_price(0, "0.50")


def test_round_trip_costs_both_sides(model):
    assert model.calculate_round_trip_cost(100, "0.50", "0.55") == D("1.75") + D("1.74")


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("price", ["0", "1"])
def test_fee_is_zero_at_price_extremes(model, price):
    """P*(1-P) vanishes at both bounds."""
    assert model.calculate_fee(100, price).fee == D(0)


def test_fractional_contracts_are_supported(model):
    """The API permits 0.01-contract granularity; fees must handle it."""
    assert model.raw_fee("2.50", "0.50", schedule=FeeSchedule()) == D("0.04375")
    assert model.calculate_fee("2.50", "0.50").fee == D("0.05")


def test_float_input_is_rejected(model):
    """Floats never enter a money path."""
    with pytest.raises(MoneyError, match="float is not accepted"):
        model.calculate_fee(100, 0.5)


def test_price_outside_range_is_rejected(model):
    with pytest.raises(MoneyError, match="outside"):
        model.calculate_fee(100, "1.01")


def test_negative_multiplier_rejected():
    with pytest.raises(MoneyError, match="non-negative"):
        FeeSchedule(taker_multiplier=D("-1"))
    with pytest.raises(MoneyError, match="non-negative"):
        FeeSchedule(maker_multiplier=D("-1"))


def test_breakdown_exposes_rounding_cost(model):
    b = model.calculate_fee(100, "0.35")
    assert b.raw == D("1.5925")
    assert b.fee == D("1.60")
    assert b.rounding_cost == D("0.0075")
    assert b.per_contract == D("0.016")


def test_non_unit_notional_scales_correctly(model):
    """Fee stays quadratic in price relative to notional for non-$1 contracts."""
    raw = model.raw_fee(100, "1.00", schedule=FeeSchedule(), notional=D(2))
    assert raw == D("0.07") * 100 * D(1) * D(1) / D(2)
