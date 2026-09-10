"""Time-series capture to SQLite.

There is **no public archive of historical order-book depth** on Kalshi. Candlesticks and
trade prints cannot reconstruct a book, so any depth-aware backtest can only ever run on data
we recorded ourselves. That is why capture exists at Phase 4, before the strategies that
consume it -- every day without it is a day of history that cannot be recovered later.

Design choices that matter for research integrity:

- **Every row carries `captured_at`**, the time *we* observed it, separate from any exchange
  timestamp. A backtest must filter on observation time, not on exchange time, or it silently
  gains lookahead.
- **Prices are stored as TEXT**, not REAL. SQLite's REAL is a binary float and would corrupt
  the sub-cent precision the API guarantees.
- **Snapshots append, never update.** Overwriting a market row would destroy the history the
  whole exercise depends on.

SQLite is the deliberate starting point: single file, no server, trivially copyable. The
schema is plain SQL so moving to Postgres later is a port, not a rewrite.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..markets.models import NormalizedEvent, NormalizedMarket

__all__ = ["MarketDataStore"]

SCHEMA = """
-- Slowly-changing reference data. Latest state per key.
CREATE TABLE IF NOT EXISTS series (
    ticker          TEXT PRIMARY KEY,
    title           TEXT NOT NULL DEFAULT '',
    category        TEXT NOT NULL DEFAULT '',
    fee_type        TEXT NOT NULL DEFAULT '',
    fee_multiplier  TEXT NOT NULL DEFAULT '1',
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_ticker        TEXT PRIMARY KEY,
    series_ticker       TEXT NOT NULL DEFAULT '',
    title               TEXT NOT NULL DEFAULT '',
    sub_title           TEXT NOT NULL DEFAULT '',
    mutually_exclusive  INTEGER NOT NULL DEFAULT 0,
    strike_date         TEXT,
    strike_period       TEXT,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS markets (
    ticker                TEXT PRIMARY KEY,
    event_ticker          TEXT NOT NULL,
    title                 TEXT NOT NULL DEFAULT '',
    yes_sub_title         TEXT NOT NULL DEFAULT '',
    status                TEXT NOT NULL,
    notional              TEXT NOT NULL DEFAULT '1',
    strike_type           TEXT,
    floor_strike          TEXT,
    cap_strike            TEXT,
    price_level_structure TEXT NOT NULL DEFAULT '',
    price_ranges          TEXT NOT NULL DEFAULT '[]',
    exchange_index        INTEGER NOT NULL DEFAULT 0,
    open_time             TEXT,
    close_time            TEXT,
    can_close_early       INTEGER NOT NULL DEFAULT 0,
    result                TEXT NOT NULL DEFAULT '',
    updated_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_markets_event ON markets(event_ticker);
CREATE INDEX IF NOT EXISTS idx_markets_status ON markets(status);

-- Append-only time series. Prices are TEXT to preserve exact decimals.
CREATE TABLE IF NOT EXISTS quote_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker         TEXT NOT NULL,
    captured_at    TEXT NOT NULL,
    status         TEXT NOT NULL,
    yes_bid        TEXT NOT NULL,
    yes_ask        TEXT NOT NULL,
    no_bid         TEXT NOT NULL,
    no_ask         TEXT NOT NULL,
    yes_bid_size   TEXT NOT NULL DEFAULT '0',
    yes_ask_size   TEXT NOT NULL DEFAULT '0',
    last_price     TEXT NOT NULL DEFAULT '0',
    volume         TEXT NOT NULL DEFAULT '0',
    open_interest  TEXT NOT NULL DEFAULT '0',
    liquidity      TEXT NOT NULL DEFAULT '0'
);
CREATE INDEX IF NOT EXISTS idx_quotes_ticker_time ON quote_snapshots(ticker, captured_at);
CREATE INDEX IF NOT EXISTS idx_quotes_time ON quote_snapshots(captured_at);

CREATE TABLE IF NOT EXISTS orderbook_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker       TEXT NOT NULL,
    captured_at  TEXT NOT NULL,
    -- Both sides are BID arrays; asks are derived. Stored as [[price, count], ...] JSON.
    yes_bids     TEXT NOT NULL,
    no_bids      TEXT NOT NULL,
    -- Records whether the source used yes-leg pricing on the no side, since the WebSocket
    -- default differs from REST and is scheduled to change.
    use_yes_price INTEGER NOT NULL DEFAULT 0,
    source       TEXT NOT NULL DEFAULT 'rest'
);
CREATE INDEX IF NOT EXISTS idx_books_ticker_time ON orderbook_snapshots(ticker, captured_at);

-- Operational record. Data quality is a first-class research output.
CREATE TABLE IF NOT EXISTS system_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at  TEXT NOT NULL,
    kind         TEXT NOT NULL,
    detail       TEXT NOT NULL DEFAULT '',
    payload      TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_sysevents_kind_time ON system_events(kind, captured_at);
"""


def _now() -> str:
    """Observation time, UTC and ISO-8601. Never local time -- backtests compare across days."""
    return datetime.now(UTC).isoformat()


def _d(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


class MarketDataStore:
    """SQLite-backed capture of markets, quotes, and order books."""

    def __init__(self, path: str | Path = "data/database/kalshi.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        # WAL lets a scanner write while a notebook reads the same file.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> MarketDataStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Group writes so a crash cannot leave a half-written snapshot sweep."""
        self._conn.execute("BEGIN")
        try:
            yield self._conn
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # -- reference data ----------------------------------------------------

    def upsert_series(
        self, ticker: str, title: str, category: str, fee_type: str, fee_multiplier: Decimal
    ) -> None:
        self._conn.execute(
            """INSERT INTO series (ticker, title, category, fee_type, fee_multiplier, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(ticker) DO UPDATE SET
                 title=excluded.title, category=excluded.category,
                 fee_type=excluded.fee_type, fee_multiplier=excluded.fee_multiplier,
                 updated_at=excluded.updated_at""",
            (ticker, title, category, fee_type, str(fee_multiplier), _now()),
        )

    def upsert_event(self, event: NormalizedEvent) -> None:
        self._conn.execute(
            """INSERT INTO events (event_ticker, series_ticker, title, sub_title,
                                   mutually_exclusive, strike_date, strike_period, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(event_ticker) DO UPDATE SET
                 series_ticker=excluded.series_ticker, title=excluded.title,
                 sub_title=excluded.sub_title,
                 mutually_exclusive=excluded.mutually_exclusive,
                 strike_date=excluded.strike_date, strike_period=excluded.strike_period,
                 updated_at=excluded.updated_at""",
            (
                event.event_ticker,
                event.series_ticker,
                event.title,
                event.sub_title,
                int(event.mutually_exclusive),
                event.strike_date.isoformat() if event.strike_date else None,
                event.strike_period,
                _now(),
            ),
        )

    def upsert_market(self, market: NormalizedMarket) -> None:
        self._conn.execute(
            """INSERT INTO markets (ticker, event_ticker, title, yes_sub_title, status,
                                    notional, strike_type, floor_strike, cap_strike,
                                    price_level_structure, price_ranges, exchange_index,
                                    open_time, close_time, can_close_early, result, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(ticker) DO UPDATE SET
                 event_ticker=excluded.event_ticker, title=excluded.title,
                 yes_sub_title=excluded.yes_sub_title, status=excluded.status,
                 notional=excluded.notional, strike_type=excluded.strike_type,
                 floor_strike=excluded.floor_strike, cap_strike=excluded.cap_strike,
                 price_level_structure=excluded.price_level_structure,
                 price_ranges=excluded.price_ranges, exchange_index=excluded.exchange_index,
                 open_time=excluded.open_time, close_time=excluded.close_time,
                 can_close_early=excluded.can_close_early, result=excluded.result,
                 updated_at=excluded.updated_at""",
            (
                market.ticker,
                market.event_ticker,
                market.title,
                market.yes_sub_title,
                market.status.value,
                str(market.notional),
                market.strike_type.value if market.strike_type else None,
                _d(market.floor_strike),
                _d(market.cap_strike),
                market.price_level_structure,
                json.dumps(
                    [
                        {"start": str(b.start), "end": str(b.end), "step": str(b.step)}
                        for b in market.price_ranges
                    ]
                ),
                market.exchange_index,
                market.open_time.isoformat() if market.open_time else None,
                market.close_time.isoformat() if market.close_time else None,
                int(market.can_close_early),
                market.result,
                _now(),
            ),
        )

    # -- time series -------------------------------------------------------

    def record_quote(self, market: NormalizedMarket, *, captured_at: str | None = None) -> None:
        """Append one top-of-book observation. Never updates an existing row."""
        self._conn.execute(
            """INSERT INTO quote_snapshots (ticker, captured_at, status, yes_bid, yes_ask,
                                            no_bid, no_ask, yes_bid_size, yes_ask_size,
                                            last_price, volume, open_interest, liquidity)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                market.ticker,
                captured_at or _now(),
                market.status.value,
                str(market.yes_bid),
                str(market.yes_ask),
                str(market.no_bid),
                str(market.no_ask),
                str(market.yes_bid_size),
                str(market.yes_ask_size),
                str(market.last_price),
                str(market.volume),
                str(market.open_interest),
                str(market.liquidity),
            ),
        )

    def record_orderbook(
        self,
        ticker: str,
        book: dict[str, Any],
        *,
        use_yes_price: bool = False,
        source: str = "rest",
        captured_at: str | None = None,
    ) -> None:
        """Append one full-depth book observation.

        Both arrays are **bids**. ``use_yes_price`` records which price scale the no side was
        reported on, because the WebSocket default differs from REST and is scheduled to flip
        -- without it, stored books become ambiguous after the migration.
        """
        self._conn.execute(
            """INSERT INTO orderbook_snapshots
                 (ticker, captured_at, yes_bids, no_bids, use_yes_price, source)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                ticker,
                captured_at or _now(),
                json.dumps(book.get("yes_dollars") or book.get("yes") or []),
                json.dumps(book.get("no_dollars") or book.get("no") or []),
                int(use_yes_price),
                source,
            ),
        )

    def record_system_event(
        self, kind: str, detail: str = "", payload: dict[str, Any] | None = None
    ) -> None:
        """Record an operational event -- throttling, API errors, invariant violations."""
        self._conn.execute(
            "INSERT INTO system_events (captured_at, kind, detail, payload) VALUES (?, ?, ?, ?)",
            (_now(), kind, detail, json.dumps(payload or {})),
        )

    def record_snapshot_sweep(
        self, events: Iterable[NormalizedEvent], *, record_quotes: bool = True
    ) -> tuple[int, int]:
        """Persist a full sweep of events and their markets in one transaction.

        Returns ``(events_written, quotes_written)``.
        """
        n_events = n_quotes = 0
        captured_at = _now()  # one timestamp for the sweep, so a scan is a coherent slice
        with self.transaction():
            for event in events:
                self.upsert_event(event)
                n_events += 1
                for market in event.markets:
                    self.upsert_market(market)
                    if record_quotes:
                        self.record_quote(market, captured_at=captured_at)
                        n_quotes += 1
        return n_events, n_quotes

    # -- reads -------------------------------------------------------------

    def counts(self) -> dict[str, int]:
        """Row counts per table, for health reporting."""
        tables = [
            "series",
            "events",
            "markets",
            "quote_snapshots",
            "orderbook_snapshots",
            "system_events",
        ]
        return {
            t: int(self._conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]) for t in tables
        }

    def quote_history(self, ticker: str, *, limit: int = 1000) -> list[sqlite3.Row]:
        """Observations for one market, oldest first."""
        return list(
            self._conn.execute(
                """SELECT * FROM quote_snapshots WHERE ticker = ?
                   ORDER BY captured_at ASC LIMIT ?""",
                (ticker, limit),
            )
        )
