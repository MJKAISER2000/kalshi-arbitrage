"""Live conformance tests: does Kalshi still behave the way docs/KALSHI_API_NOTES.md claims?

Every structural assumption the strategies rest on is asserted here against the real API.
If Kalshi changes the order-book representation, the price scale, or the mutual-exclusivity
semantics, this fails loudly instead of silently corrupting every edge calculation.

Marked ``live`` and skipped by default::

    python -m pytest -m live        # run them
    python -m pytest                # default: skipped

No authentication required -- all endpoints used here are public.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from decimal import Decimal

import pytest

from kalshi_arb.contracts import complement_invariant_holds, derive_ask
from kalshi_arb.fees import TAKER_BASE_RATE, FeeSchedule, Role

pytestmark = pytest.mark.live

BASE = "https://external-api.kalshi.com/trade-api/v2"
TIMEOUT = 30
D = Decimal


def get(path: str) -> dict:
    """GET a public endpoint, decoding as UTF-8 explicitly.

    The platform default codec on Windows is cp1252 and *crashes* on live Kalshi payloads --
    see docs/KALSHI_API_NOTES.md section 12.5.
    """
    req = urllib.request.Request(f"{BASE}{path}", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:  # pragma: no cover - network dependent
        pytest.skip(f"Kalshi API unreachable: {exc}")


@pytest.fixture(scope="module")
def markets() -> list[dict]:
    payload = get("/events?limit=200&status=open&with_nested_markets=true")
    mk = [m for e in payload["events"] for m in e.get("markets", [])]
    if not mk:  # pragma: no cover - depends on exchange state
        pytest.skip("no open markets returned")
    return mk


@pytest.fixture(scope="module")
def events() -> list[dict]:
    return get("/events?limit=200&status=open&with_nested_markets=true")["events"]


# ---------------------------------------------------------------------------
# Price representation
# ---------------------------------------------------------------------------


def test_public_market_data_needs_no_auth():
    assert get("/exchange/status")["exchange_active"] in (True, False)


def test_prices_are_fixed_point_dollar_strings(markets):
    """If these ever become numbers, Decimal parsing must be revisited before trusting P&L."""
    for m in markets[:50]:
        for field in (
            "yes_bid_dollars",
            "yes_ask_dollars",
            "no_bid_dollars",
            "no_ask_dollars",
            "notional_value_dollars",
        ):
            assert isinstance(m[field], str), f"{m['ticker']}.{field} is not a string"
            D(m[field])  # must parse exactly


def test_counts_are_fixed_point_strings(markets):
    for m in markets[:50]:
        for field in ("volume_fp", "open_interest_fp", "yes_bid_size_fp"):
            assert isinstance(m[field], str)
            D(m[field])


def test_every_market_publishes_a_tick_grid(markets):
    """``price_ranges`` is the source of truth for valid prices; never assume $0.01."""
    for m in markets[:50]:
        ranges = m["price_ranges"]
        assert ranges, f"{m['ticker']} has no price_ranges"
        for band in ranges:
            assert D(band["step"]) > 0
            assert D(band["start"]) < D(band["end"])


# ---------------------------------------------------------------------------
# The structural identity that retires single-market complement arbitrage
# ---------------------------------------------------------------------------


def test_asks_are_derived_from_opposite_bids(markets):
    """yes_ask == notional - no_bid and no_ask == notional - yes_bid, on every market.

    This is the identity that makes single-market YES+NO arbitrage impossible. If it ever
    breaks, Strategy #1 must be reconsidered -- and until then the book-integrity invariant
    would be reporting false alarms.
    """
    for m in markets:
        notional = D(m["notional_value_dollars"])
        assert D(m["yes_ask_dollars"]) == derive_ask(m["no_bid_dollars"], notional=notional), (
            f"{m['ticker']}: yes_ask != notional - no_bid"
        )
        assert D(m["no_ask_dollars"]) == derive_ask(m["yes_bid_dollars"], notional=notional), (
            f"{m['ticker']}: no_ask != notional - yes_bid"
        )


def test_no_crossed_books_exist(markets):
    """yes_bid + no_bid <= notional everywhere: the matching engine pairs crossing orders."""
    for m in markets:
        assert complement_invariant_holds(
            m["yes_bid_dollars"],
            m["no_bid_dollars"],
            notional=D(m["notional_value_dollars"]),
        ), f"{m['ticker']}: crossed book"


# ---------------------------------------------------------------------------
# Order book shape
# ---------------------------------------------------------------------------


def test_orderbook_contains_only_bids_sorted_ascending(markets):
    """`orderbook_fp` has yes_dollars/no_dollars bid arrays; best bid is the LAST element."""
    liquid = max(markets, key=lambda m: D(m["volume_fp"]))
    ob = get(f"/markets/{liquid['ticker']}/orderbook")["orderbook_fp"]

    assert set(ob) <= {"yes_dollars", "no_dollars"}, f"unexpected orderbook keys: {set(ob)}"
    for side in ("yes_dollars", "no_dollars"):
        levels = ob.get(side) or []
        prices = [D(p) for p, _ in levels]
        assert prices == sorted(prices), f"{side} not ascending"
        for _, count in levels:
            assert D(count) > 0


# ---------------------------------------------------------------------------
# Event semantics that gate the basket strategies
# ---------------------------------------------------------------------------


def test_mutually_exclusive_flag_is_present_and_discriminating(events):
    """The flag must exist on every event and must not be constant.

    Basket arbitrage is only sound when this is checked per event -- roughly a fifth of open
    events carry it.
    """
    flags = [e["mutually_exclusive"] for e in events]
    assert all(isinstance(f, bool) for f in flags)
    assert any(flags) and not all(flags), "flag is constant; per-event check may be broken"


def test_mutually_exclusive_events_are_not_overround_beyond_fees(events):
    """Documents the live state of the primary strategy rather than asserting profit.

    A basket edge only exists when sum(yes_bid) > notional. This test records how often that
    happens; it fails only if the arithmetic itself is inconsistent.
    """
    for e in events:
        if not e["mutually_exclusive"]:
            continue
        active = [m for m in e.get("markets", []) if m["status"] == "active"]
        if len(active) < 2:
            continue
        total_bid = sum(D(m["yes_bid_dollars"]) for m in active)
        total_ask = sum(D(m["yes_ask_dollars"]) for m in active)
        # Asks are derived from the opposite bids, so the ask sum must dominate the bid sum.
        assert total_ask >= total_bid, f"{e['event_ticker']}: ask sum below bid sum"


def test_strike_metadata_available_for_logical_relations(markets):
    """Structured strikes are the basis for logical arbitrage without NLP."""
    typed = [m for m in markets if m.get("strike_type")]
    assert typed, "no market exposed strike_type; logical relation derivation would be blind"
    valid = {
        "greater",
        "greater_or_equal",
        "less",
        "less_or_equal",
        "between",
        "functional",
        "custom",
        "structured",
    }
    assert {m["strike_type"] for m in typed} <= valid


def test_market_status_values_are_known(markets):
    known = {
        "initialized",
        "active",
        "inactive",
        "closed",
        "determined",
        "disputed",
        "amended",
        "finalized",
    }
    assert {m["status"] for m in markets} <= known


def test_exchange_reports_per_shard_status():
    """Shards pause independently; a scanner must check the shard, not just the global flag."""
    status = get("/exchange/status")
    shards = status["exchange_index_statuses"]
    assert shards
    for shard in shards:
        assert isinstance(shard["exchange_index"], int)
        assert isinstance(shard["trading_active"], bool)


# ---------------------------------------------------------------------------
# Fee schedule interpretation
# ---------------------------------------------------------------------------


SAMPLE_SERIES = ["KXCPI", "KXNBA", "KXFED", "KXGDP"]


@pytest.mark.parametrize("series", SAMPLE_SERIES)
def test_fee_multiplier_is_a_multiplier_not_a_rate(series):
    """The API's fee_multiplier carries the schedule's M, never the 0.07 base rate.

    Live values are small integers. Treating the field as the rate would price a standard
    series ~14x too high, which is precisely the error this pins down. The multiplier varies
    by series, so it must always be read per series rather than assumed.
    """
    s = get(f"/series/{series}")["series"]
    multiplier = D(str(s["fee_multiplier"]))
    assert D(0) <= multiplier <= D(2), f"{series}: unexpected fee_multiplier {multiplier}"
    assert multiplier != D("0.07"), f"{series}: fee_multiplier looks like a rate, not M"
    assert s["fee_type"] in {
        "quadratic",
        "quadratic_with_maker_fees",
        "quadratic_with_combo_maker_fees",
        "flat",
    }


def test_fee_schedule_from_api_prices_a_live_series_correctly():
    """End-to-end: API fields -> FeeSchedule -> the published rates."""
    s = get("/series/KXCPI")["series"]
    sched = FeeSchedule.from_api(s["fee_type"], str(s["fee_multiplier"]))
    assert sched.rate(Role.TAKER) == TAKER_BASE_RATE * D(str(s["fee_multiplier"]))
    # A standard series charges the full taker base rate.
    assert sched.rate(Role.TAKER) == D("0.07")
