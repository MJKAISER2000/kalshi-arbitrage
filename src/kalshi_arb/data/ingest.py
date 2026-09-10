"""Market ingestion.

Sweeps the exchange and records what it sees. Two properties are deliberate:

- **Shard-aware.** Kalshi shards the exchange and each shard pauses independently, so a sweep
  records per-shard trading status rather than only the global flag. A market on a paused
  shard is not tradeable even when the exchange reports itself active.
- **Invariant-checking on ingest.** Every quote is checked against
  ``yes_bid + no_bid <= notional``. A violation cannot be an opportunity -- the matching
  engine prevents crossed books -- so it is recorded as a data-quality fault. This is the
  retired single-market "arbitrage" earning its keep as a bug detector.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..api.client import KalshiClient
from ..markets.models import NormalizedEvent
from .storage import MarketDataStore

__all__ = ["IngestReport", "MarketIngestor"]


@dataclass
class IngestReport:
    """What one sweep saw. Persisted alongside the data as the audit trail."""

    events: int = 0
    markets: int = 0
    quotes: int = 0
    books: int = 0
    mutually_exclusive_events: int = 0
    tradeable_markets: int = 0
    crossed_books: list[str] = field(default_factory=list)
    """Markets violating the complement invariant -- a data fault, never an opportunity."""
    inactive_shards: list[int] = field(default_factory=list)
    throttled_seconds: float = 0.0

    @property
    def is_healthy(self) -> bool:
        return not self.crossed_books and not self.inactive_shards

    def summary(self) -> str:
        parts = [
            f"{self.events} events ({self.mutually_exclusive_events} mutually exclusive)",
            f"{self.markets} markets ({self.tradeable_markets} tradeable)",
            f"{self.quotes} quotes",
        ]
        if self.books:
            parts.append(f"{self.books} books")
        if self.throttled_seconds > 0.05:
            parts.append(f"throttled {self.throttled_seconds:.1f}s")
        if self.crossed_books:
            parts.append(f"CROSSED BOOKS: {len(self.crossed_books)}")
        if self.inactive_shards:
            parts.append(f"inactive shards: {self.inactive_shards}")
        return ", ".join(parts)


class MarketIngestor:
    """Captures market state into the store."""

    def __init__(self, client: KalshiClient, store: MarketDataStore) -> None:
        self._client = client
        self._store = store

    async def check_shards(self) -> list[int]:
        """Shard indices that are not currently trading."""
        status = await self._client.get_exchange_status()
        inactive = [
            int(s["exchange_index"])
            for s in status.get("exchange_index_statuses", [])
            if not s.get("trading_active", False)
        ]
        if not status.get("trading_active", True):
            self._store.record_system_event("exchange_inactive", "exchange-wide halt", status)
        return inactive

    def _audit(self, events: list[NormalizedEvent], report: IngestReport) -> None:
        """Check ingest-time invariants and count what we saw."""
        for event in events:
            report.events += 1
            if event.mutually_exclusive:
                report.mutually_exclusive_events += 1
            for market in event.markets:
                report.markets += 1
                if market.status.is_tradeable:
                    report.tradeable_markets += 1
                if not market.book_is_uncrossed:
                    report.crossed_books.append(market.ticker)
                    self._store.record_system_event(
                        "invariant_violation",
                        f"{market.ticker}: yes_bid + no_bid exceeds notional",
                        {
                            "ticker": market.ticker,
                            "yes_bid": str(market.yes_bid),
                            "no_bid": str(market.no_bid),
                            "notional": str(market.notional),
                        },
                    )

    async def sweep(self, *, status: str = "open", max_pages: int | None = None) -> IngestReport:
        """Capture every event and market matching ``status``.

        A sweep shares one capture timestamp across its rows so a scan reads back as a
        coherent slice rather than a smear across the time it took to fetch.
        """
        report = IngestReport()
        before = self._client.throttled_seconds

        report.inactive_shards = await self.check_shards()
        events = await self._client.get_events(status=status, nested=True, max_pages=max_pages)

        self._audit(events, report)
        _, quotes = self._store.record_snapshot_sweep(events)
        report.quotes = quotes
        report.throttled_seconds = self._client.throttled_seconds - before

        self._store.record_system_event(
            "sweep_complete",
            report.summary(),
            {
                "events": report.events,
                "markets": report.markets,
                "quotes": report.quotes,
                "crossed_books": report.crossed_books,
                "inactive_shards": report.inactive_shards,
            },
        )
        return report

    async def capture_books(self, tickers: list[str], report: IngestReport | None = None) -> int:
        """Record full-depth books for specific markets.

        Books are the data no public archive provides, so this is the part of capture that
        cannot be backfilled later. Rate limiting means it is targeted rather than exhaustive:
        at Basic tier a full-exchange book sweep is not feasible.
        """
        count = 0
        for ticker in tickers:
            book = await self._client.get_orderbook(ticker)
            self._store.record_orderbook(ticker, book, source="rest")
            count += 1
        if report is not None:
            report.books += count
        return count
