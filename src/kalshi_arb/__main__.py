"""Command line interface.

    python -m kalshi_arb health     exchange status, config, and the trading-mode gate
    python -m kalshi_arb ingest     sweep markets and record a snapshot
    python -m kalshi_arb books      capture full-depth order books for given tickers
    python -m kalshi_arb stats      what the local database holds

Every command prints the effective trading mode first. Live trading is not implemented, and
the gate is reported on every run rather than assumed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from .api.client import KalshiClient
from .config import ConfigError, Settings, load_settings
from .data.ingest import MarketIngestor
from .data.storage import MarketDataStore


def _banner(settings: Settings) -> None:
    print(f"mode: {settings.mode.value}  |  environment: {settings.environment.value}", end="")
    print(f"  |  tier: {settings.api_tier}")
    if settings.gate_refusal:
        print(f"  live trading REFUSED: {settings.gate_refusal}", file=sys.stderr)
    print()


async def cmd_health(settings: Settings, args: argparse.Namespace) -> int:
    async with KalshiClient(settings) as client:
        status = await client.get_exchange_status()
        active = status.get("trading_active", False)
        print(f"exchange trading_active: {active}")
        for shard in status.get("exchange_index_statuses", []):
            flag = "OK  " if shard.get("trading_active") else "HALT"
            print(f"  [{flag}] shard {shard.get('exchange_index')}: {shard.get('description', '')}")
    with MarketDataStore(args.database) as store:
        print(f"\ndatabase: {store.path}")
        for table, n in store.counts().items():
            print(f"  {table:22s} {n:>9,}")
    return 0


async def cmd_ingest(settings: Settings, args: argparse.Namespace) -> int:
    with MarketDataStore(args.database) as store:
        async with KalshiClient(settings) as client:
            report = await MarketIngestor(client, store).sweep(
                status=args.status, max_pages=args.max_pages
            )
        print(report.summary())
        if report.crossed_books:
            print(
                "\nWARNING: crossed books detected. This is a data fault, not an "
                "opportunity -- the matching engine prevents crossed books, so a violation "
                "means stale or mis-parsed data:",
                file=sys.stderr,
            )
            for ticker in report.crossed_books[:10]:
                print(f"  {ticker}", file=sys.stderr)
        return 0 if report.is_healthy else 1


async def cmd_books(settings: Settings, args: argparse.Namespace) -> int:
    with MarketDataStore(args.database) as store:
        async with KalshiClient(settings) as client:
            n = await MarketIngestor(client, store).capture_books(args.tickers)
    print(f"captured {n} order books")
    return 0


async def cmd_stats(settings: Settings, args: argparse.Namespace) -> int:
    with MarketDataStore(args.database) as store:
        counts = store.counts()
        print(f"database: {store.path}")
        for table, n in counts.items():
            print(f"  {table:22s} {n:>9,}")
        if counts["quote_snapshots"] == 0:
            print("\nNo observations recorded yet. Run: python -m kalshi_arb ingest")
    return 0


COMMANDS = {
    "health": cmd_health,
    "ingest": cmd_ingest,
    "books": cmd_books,
    "stats": cmd_stats,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kalshi_arb", description=__doc__)
    parser.add_argument("--config", default="config/settings.yaml", help="settings YAML path")
    parser.add_argument(
        "--database", default="data/database/kalshi.db", help="SQLite database path"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("health", help="exchange status, config, and database health")

    ingest = sub.add_parser("ingest", help="sweep markets and record a snapshot")
    ingest.add_argument("--status", default="open", help="market status filter")
    ingest.add_argument(
        "--max-pages", type=int, default=None, help="stop after N pages (for a quick sample)"
    )

    books = sub.add_parser("books", help="capture full-depth order books")
    books.add_argument("tickers", nargs="+", help="market tickers")

    sub.add_parser("stats", help="what the local database holds")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(args.config)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    _banner(settings)
    try:
        return asyncio.run(COMMANDS[args.command](settings, args))
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
