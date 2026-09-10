"""Normalized model tests.

These cover the boundary where API payloads become domain objects. The important cases are
the ones that silently corrupt downstream math: floats where decimals belong, and derived asks
that look like prices but mean "no liquidity".

Payload shapes are taken from real production responses.
"""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from kalshi_arb.contracts import Exclusivity
from kalshi_arb.markets.models import (
    MarketStatus,
    NormalizedEvent,
    NormalizedMarket,
    NormalizedSeries,
    PriceBand,
    StrikeType,
)

D = Decimal


def market(**overrides) -> NormalizedMarket:
    payload = {
        "ticker": "KXTEST-A",
        "event_ticker": "KXTEST",
        "status": "active",
        "notional_value_dollars": "1.0000",
        "yes_bid_dollars": "0.4200",
        "yes_ask_dollars": "0.4400",
        "no_bid_dollars": "0.5600",
        "no_ask_dollars": "0.5800",
        "yes_bid_size_fp": "13.00",
        "volume_fp": "100.00",
    }
    payload.update(overrides)
    return NormalizedMarket.model_validate(payload)


# ---------------------------------------------------------------------------
# Fixed-point parsing
# ---------------------------------------------------------------------------


def test_prices_parse_to_exact_decimals():
    m = market()
    assert m.yes_bid == D("0.42")
    assert isinstance(m.yes_bid, Decimal)
    assert m.yes_bid_size == D("13")


def test_sub_cent_precision_is_preserved():
    """Some markets tick at $0.0001; losing that precision misprices every edge there."""
    m = market(yes_bid_dollars="0.4237", yes_ask_dollars="0.4238")
    assert m.yes_bid == D("0.4237")
    assert m.spread == D("0.0001")


def test_float_price_is_rejected():
    """A float would silently lose the precision the API guarantees."""
    with pytest.raises(ValidationError, match="float is not accepted"):
        market(yes_bid_dollars=0.42)


def test_fractional_contract_counts_parse():
    assert market(volume_fp="2.50").volume == D("2.50")


def test_unknown_fields_are_ignored():
    """Kalshi adds fields regularly; a scanner that dies on one is worse than one that skips it."""
    m = market(some_brand_new_field="surprise")
    assert m.ticker == "KXTEST-A"


def test_missing_optional_fields_get_safe_defaults():
    m = NormalizedMarket.model_validate({"ticker": "T", "event_ticker": "E", "status": "active"})
    assert m.yes_bid == D(0)
    assert m.volume == D(0)
    assert m.strike_type is None


# ---------------------------------------------------------------------------
# The ambiguous-ask trap
# ---------------------------------------------------------------------------


def test_ask_at_notional_with_no_opposite_bid_is_not_a_real_quote():
    """An empty NO book renders as yes_ask == $1.00, which is not a price you can hit."""
    m = market(yes_ask_dollars="1.0000", no_bid_dollars="0.0000")
    assert m.yes_ask == D(1)
    assert not m.has_yes_ask
    assert not m.is_two_sided


def test_zero_ask_is_not_a_free_contract():
    """yes_ask == 0 means a NO bid at $1.00, not a giveaway."""
    m = market(yes_ask_dollars="0.0000", no_bid_dollars="1.0000")
    assert m.has_yes_ask  # a real bid exists on the other side
    assert m.yes_ask == D(0)


def test_two_sided_market_is_recognised():
    assert market().is_two_sided


def test_complement_invariant_exposed_on_the_model():
    assert market().book_is_uncrossed
    assert not market(yes_bid_dollars="0.60", no_bid_dollars="0.45").book_is_uncrossed


def test_spread_and_mid():
    m = market()
    assert m.spread == D("0.02")
    assert m.mid == D("0.43")


# ---------------------------------------------------------------------------
# Tick grid
# ---------------------------------------------------------------------------


def test_tick_size_is_read_from_price_ranges_not_assumed():
    """Observed live: a market whose tick is $0.0001, not $0.01."""
    m = market(
        price_ranges=[
            {"start": "0.0000", "end": "0.0100", "step": "0.0001"},
            {"start": "0.0100", "end": "0.9900", "step": "0.0010"},
        ]
    )
    assert m.tick_at(D("0.005")) == D("0.0001")
    assert m.tick_at(D("0.50")) == D("0.0010")


def test_off_grid_prices_are_invalid():
    m = market(price_ranges=[{"start": "0.0000", "end": "1.0000", "step": "0.0100"}])
    assert m.is_valid_price(D("0.42"))
    assert not m.is_valid_price(D("0.4237"))


def test_price_band_grid_check():
    band = PriceBand(start=D("0.10"), end=D("0.90"), step=D("0.01"))
    assert band.is_on_grid(D("0.42"))
    assert not band.is_on_grid(D("0.425"))
    assert not band.is_on_grid(D("0.05"))  # outside the band


def test_market_without_price_ranges_falls_back_to_bounds():
    m = market()
    assert m.is_valid_price(D("0.42"))
    assert not m.is_valid_price(D("1.5"))


# ---------------------------------------------------------------------------
# Status and strikes
# ---------------------------------------------------------------------------


def test_only_active_is_tradeable():
    assert MarketStatus.ACTIVE.is_tradeable
    for status in MarketStatus:
        if status is not MarketStatus.ACTIVE:
            assert not status.is_tradeable


def test_inactive_is_a_pause_not_a_close():
    """Reactivation cancels resting orders, so a pause is a live legging risk."""
    m = market(status="inactive")
    assert m.status is MarketStatus.INACTIVE
    assert not m.status.is_tradeable


def test_threshold_strikes_support_monotone_ladders():
    for st in (
        StrikeType.GREATER,
        StrikeType.GREATER_OR_EQUAL,
        StrikeType.LESS,
        StrikeType.LESS_OR_EQUAL,
    ):
        assert st.is_monotone_threshold
    assert not StrikeType.CUSTOM.is_monotone_threshold


def test_strike_fields_parse():
    m = market(strike_type="greater_or_equal", floor_strike="60.0")
    assert m.strike_type is StrikeType.GREATER_OR_EQUAL
    assert m.floor_strike == D("60.0")


def test_blank_strike_type_becomes_none():
    assert market(strike_type="").strike_type is None


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def event(mutually_exclusive: bool, *bids: str) -> NormalizedEvent:
    return NormalizedEvent.model_validate(
        {
            "event_ticker": "KXTEST",
            "mutually_exclusive": mutually_exclusive,
            "markets": [
                {
                    "ticker": f"KXTEST-{i}",
                    "event_ticker": "KXTEST",
                    "status": "active",
                    "yes_bid_dollars": b,
                    "yes_ask_dollars": str(D(b) + D("0.02")),
                }
                for i, b in enumerate(bids)
            ],
        }
    )


def test_mutually_exclusive_never_maps_to_partition():
    """The flag proves AT MOST one YES. Assuming exhaustiveness is the expensive mistake."""
    e = event(True, "0.30", "0.40")
    assert e.exclusivity is Exclusivity.MUTUALLY_EXCLUSIVE
    assert e.exclusivity is not Exclusivity.PARTITION


def test_non_exclusive_event_is_unconstrained():
    assert event(False, "0.30", "0.40").exclusivity is Exclusivity.UNCONSTRAINED


def test_bid_and_ask_sums_detect_over_and_underround():
    e = event(True, "0.50", "0.60")
    assert e.yes_bid_sum() == D("1.10")  # overround: the safe direction
    assert e.yes_ask_sum() == D("1.14")


def test_only_active_markets_count_toward_sums():
    e = event(True, "0.50", "0.60")
    e.markets[1].status = MarketStatus.CLOSED
    assert len(e.active_markets) == 1
    assert e.yes_bid_sum() == D("0.50")


def test_empty_event_sums_to_zero():
    assert event(True).yes_bid_sum() == D(0)


# ---------------------------------------------------------------------------
# Series
# ---------------------------------------------------------------------------


def test_series_fee_multiplier_becomes_decimal_without_float_error():
    """The API sends fee_multiplier as a JSON number; it must not reach fee math as a float."""
    s = NormalizedSeries.model_validate(
        {"ticker": "KXTEST", "fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}
    )
    assert s.fee_multiplier == D(1)
    assert isinstance(s.fee_multiplier, Decimal)


def test_series_zero_multiplier_parses():
    s = NormalizedSeries.model_validate(
        {"ticker": "KXTEST", "fee_type": "quadratic", "fee_multiplier": 0}
    )
    assert s.fee_multiplier == D(0)
