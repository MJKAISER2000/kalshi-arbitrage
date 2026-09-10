"""Storage tests.

Order-book history cannot be bought or backfilled -- it only exists if we recorded it -- so
the properties that protect that record are tested hardest: exact decimal round-trips,
append-only snapshots, and an observation timestamp distinct from exchange time.
"""

from decimal import Decimal

import pytest

from kalshi_arb.data.storage import MarketDataStore
from kalshi_arb.markets.models import NormalizedEvent, NormalizedMarket

D = Decimal


@pytest.fixture
def store(tmp_path) -> MarketDataStore:
    with MarketDataStore(tmp_path / "test.db") as s:
        yield s


def make_market(ticker: str = "KXTEST-A", **overrides) -> NormalizedMarket:
    payload = {
        "ticker": ticker,
        "event_ticker": "KXTEST",
        "status": "active",
        "yes_bid_dollars": "0.4237",
        "yes_ask_dollars": "0.4400",
        "no_bid_dollars": "0.5600",
        "no_ask_dollars": "0.5763",
        "volume_fp": "13.50",
        "price_ranges": [{"start": "0.0000", "end": "1.0000", "step": "0.0100"}],
    }
    payload.update(overrides)
    return NormalizedMarket.model_validate(payload)


def make_event(*tickers: str) -> NormalizedEvent:
    return NormalizedEvent.model_validate(
        {
            "event_ticker": "KXTEST",
            "series_ticker": "KXTEST",
            "mutually_exclusive": True,
            "markets": [make_market(t).model_dump(by_alias=False) for t in tickers],
        }
    )


# ---------------------------------------------------------------------------
# Schema and round-tripping
# ---------------------------------------------------------------------------


def test_schema_is_created_on_open(store):
    assert set(store.counts()) >= {
        "series",
        "events",
        "markets",
        "quote_snapshots",
        "orderbook_snapshots",
        "system_events",
    }
    assert all(n == 0 for n in store.counts().values())


def test_prices_round_trip_with_exact_precision(store):
    """SQLite REAL is a binary float and would corrupt sub-cent prices; we store TEXT."""
    store.record_quote(make_market())
    row = store.quote_history("KXTEST-A")[0]
    assert D(row["yes_bid"]) == D("0.4237")
    assert D(row["no_ask"]) == D("0.5763")


def test_fractional_counts_round_trip(store):
    store.record_quote(make_market(volume_fp="13.50"))
    assert D(store.quote_history("KXTEST-A")[0]["volume"]) == D("13.50")


def test_market_upsert_updates_rather_than_duplicating(store):
    store.upsert_market(make_market())
    store.upsert_market(make_market(status="closed"))
    assert store.counts()["markets"] == 1


def test_price_ranges_survive_as_json(store):
    store.upsert_market(make_market())
    row = store._conn.execute("SELECT price_ranges FROM markets").fetchone()
    assert "0.0100" in row["price_ranges"]


# ---------------------------------------------------------------------------
# Append-only history
# ---------------------------------------------------------------------------


def test_quotes_append_and_never_overwrite(store):
    """Overwriting a quote would destroy the history the whole exercise depends on."""
    for bid in ("0.42", "0.43", "0.44"):
        store.record_quote(make_market(yes_bid_dollars=bid))
    history = store.quote_history("KXTEST-A")
    assert len(history) == 3
    assert [r["yes_bid"] for r in history] == ["0.42", "0.43", "0.44"]


def test_every_row_carries_an_observation_timestamp(store):
    """Backtests must filter on when WE saw it, or they gain lookahead."""
    store.record_quote(make_market())
    row = store.quote_history("KXTEST-A")[0]
    assert row["captured_at"]
    assert row["captured_at"].endswith("+00:00")  # UTC, not local time


def test_sweep_shares_one_timestamp_across_its_rows(store):
    """A sweep should read back as a coherent slice, not a smear across fetch time."""
    store.record_snapshot_sweep([make_event("A", "B", "C")])
    stamps = {
        r["captured_at"] for r in store._conn.execute("SELECT captured_at FROM quote_snapshots")
    }
    assert len(stamps) == 1


def test_sweep_records_events_markets_and_quotes(store):
    n_events, n_quotes = store.record_snapshot_sweep([make_event("A", "B")])
    assert (n_events, n_quotes) == (1, 2)
    counts = store.counts()
    assert counts["events"] == 1
    assert counts["markets"] == 2
    assert counts["quote_snapshots"] == 2


def test_repeated_sweeps_accumulate_quotes_but_not_markets(store):
    event = make_event("A", "B")
    store.record_snapshot_sweep([event])
    store.record_snapshot_sweep([event])
    counts = store.counts()
    assert counts["markets"] == 2  # reference data is current-state
    assert counts["quote_snapshots"] == 4  # observations accumulate


# ---------------------------------------------------------------------------
# Order books
# ---------------------------------------------------------------------------


def test_orderbook_stores_both_bid_arrays(store):
    store.record_orderbook(
        "KXTEST-A",
        {"yes_dollars": [["0.42", "13.00"]], "no_dollars": [["0.56", "17.00"]]},
    )
    row = store._conn.execute("SELECT * FROM orderbook_snapshots").fetchone()
    assert "0.42" in row["yes_bids"]
    assert "0.56" in row["no_bids"]


def test_orderbook_records_the_price_scale_used(store):
    """The WebSocket no-side scale differs from REST and is scheduled to flip.

    Without recording which scale applied, stored books become ambiguous after the migration.
    """
    store.record_orderbook("A", {}, use_yes_price=True, source="websocket")
    row = store._conn.execute("SELECT * FROM orderbook_snapshots").fetchone()
    assert row["use_yes_price"] == 1
    assert row["source"] == "websocket"


def test_empty_orderbook_is_recorded_not_skipped(store):
    """An empty book is a real observation -- it says there was no liquidity then."""
    store.record_orderbook("KXTEST-A", {})
    assert store.counts()["orderbook_snapshots"] == 1


# ---------------------------------------------------------------------------
# Operational record
# ---------------------------------------------------------------------------


def test_system_events_are_recorded_with_payload(store):
    store.record_system_event("invariant_violation", "crossed book", {"ticker": "KXTEST-A"})
    row = store._conn.execute("SELECT * FROM system_events").fetchone()
    assert row["kind"] == "invariant_violation"
    assert "KXTEST-A" in row["payload"]


def test_transaction_rolls_back_on_failure(store):
    """A crash mid-sweep must not leave a half-written snapshot."""
    with pytest.raises(RuntimeError), store.transaction():
        store.record_quote(make_market())
        raise RuntimeError("boom")
    assert store.counts()["quote_snapshots"] == 0


def test_database_file_is_created_with_parent_directories(tmp_path):
    path = tmp_path / "nested" / "deeper" / "kalshi.db"
    with MarketDataStore(path) as s:
        assert s.path.exists()
