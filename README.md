# Kalshi Arbitrage Research Platform

A research platform for finding and — more often — **disproving** arbitrage and relative-value
opportunities on [Kalshi](https://kalshi.com).

The question it is built to answer is not *"can we find arbitrage?"* but:

> After fees, liquidity, latency, partial fills, legging risk, market impact and capital
> constraints, does an economically meaningful and repeatable edge actually exist?

**No claim of profitability is made.** Early research results have been largely negative, and
that is the point — see [Research findings](#research-findings).

---

## Status

**Phase 4 of 17 complete.** Implemented and tested: money primitives, binary contract
mathematics, basket payoff proofs, the fee engine, and market ingestion (REST client with
RSA-PSS signing, tier-aware rate limiting, normalized models, and SQLite capture). Order-book
reconstruction, scanners, backtesting, paper trading, and the dashboard are not built yet.

| | |
|---|---|
| Unit + property tests | **215 passing** |
| Live API conformance tests | **17 passing** against production |
| Live trading | **disabled and unimplemented** |

A live sweep captures ~600 events and ~4,250 markets in about 3 seconds.

Directories for later phases exist but are empty. Code arrives when its phase starts.

---

## Research findings

Both results below came from the Phase 1 API research and are reproduced as regression tests.
They document **exchange mechanics**; strategy-specific conclusions are kept private.

### 1. Single-market YES/NO arbitrage is structurally impossible

The usual first strategy — look for `yes_ask + no_ask < $1` — cannot work on Kalshi.

The exchange publishes **only bids**. Asks are derived: `yes_ask = notional − no_bid`. So

```
profit = notional − (yes_ask + no_ask) = (yes_bid + no_bid) − notional = −spread
```

The "arbitrage" condition is algebraically identical to a **crossed book**, which the matching
engine prevents — the two orders are each other's counterparty.

Tested against **1,730 live markets: the identity held 1,730/1,730, zero crossings.**

The strategy is retired and repurposed as a **book-integrity invariant**: a violation means
stale data or a parsing bug (for instance mixing the WebSocket's no-leg and yes-leg price
scales), and is routed to data quality — never to the opportunity table.

### 2. Fees, not prices, decide multi-leg viability

Kalshi charges trading fees **per order**, each rounded up independently, and the fee is
quadratic in price (peaking at $0.50). So an N-leg basket pays N separate round-ups while the
arbitrage edge does not scale with N.

Measured against live multi-outcome events, this is frequently the difference between a real
gross edge and a net loss. The consequence for the design is that the fee model is a **gate**
evaluated before an opportunity is ever reported — not a column added to the output.

Specific measured opportunities and the conditions under which edge survives are kept in
private research notes and are not published here.

---

## Architecture

Layers depend only downward; the money math at the bottom has no I/O and is exhaustively
testable in isolation. Full diagram in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

```
cli / dashboard -> backtest | paper | live -> portfolio + risk -> strategies
   -> arbitrage | execution | relationships -> orderbook + markets
   -> contracts + fees -> types
                       ^- api (REST, auth, rate limits) + data (capture, storage)
```

| Document | Contents |
|---|---|
| [docs/KALSHI_API_NOTES.md](docs/KALSHI_API_NOTES.md) | API research, every claim tagged verified / documented / unverified |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Layering, money representation, trading-mode gate |
| [docs/STRATEGIES.md](docs/STRATEGIES.md) | Strategy classes, payoff proofs, rejection taxonomy |

---

## Installation

Requires Python 3.11+.

```bash
python -m pip install -e ".[dev]"
```

```bash
cp .env.example .env
```

Then edit `.env`. Credentials come **only** from environment variables; `.env`, `*.pem` and
`*.key` are gitignored. Keep the RSA private key outside the repository.

| Variable | Meaning |
|---|---|
| `KALSHI_API_KEY_ID` | API key ID |
| `KALSHI_PRIVATE_KEY_PATH` | Path to the RSA private key (outside the repo) |
| `KALSHI_ENVIRONMENT` | `demo` or `production` — credentials are not shared |
| `TRADING_MODE` | `research` / `backtest` / `paper` / `live` (default `paper`) |
| `ENABLE_LIVE_TRADING` | Must be `true` *and* mode `live` *and* the live gate must pass |
| `KALSHI_API_TIER` | Sets the rate-limit budget the client self-enforces |

Strategy parameters live in [config/settings.yaml](config/settings.yaml).

---

## Usage

```bash
python -m kalshi_arb health
```

Reports the effective trading mode, per-shard exchange status, and what the local database
holds. Every command prints the mode first — the gate is reported, never assumed.

```bash
python -m kalshi_arb ingest
```

Sweeps open markets and records a snapshot. Add `--max-pages 3` for a quick sample. Exits
non-zero if a data-quality fault is found.

```bash
python -m kalshi_arb books KXSOMEMARKET-TICKER
```

Captures full-depth order books. **This is the data no public archive provides** — see
Known limitations.

```bash
python -m kalshi_arb stats
```

---

## Testing

```bash
python -m pytest
```

Live conformance tests are skipped by default. They assert that Kalshi still behaves the way
`KALSHI_API_NOTES.md` documents, so an upstream change fails a test rather than silently
corrupting every edge calculation. They need no credentials:

```bash
python -m pytest -m live
```

Every financial calculation has unit tests, including the full **published General Trading
Fees Table** from the official schedule. Property-based tests (Hypothesis) cover the invariants: binary payoff
consistency, basket payoff bounds, fee monotonicity and symmetry, and per-order rounding.

---

## Risk controls

Live trading **fails closed**. It requires all of: `TRADING_MODE=live`,
`ENABLE_LIVE_TRADING=true`, a passing live gate (minimum paper trades and duration, positive
expectancy, controlled drawdown, low hedge-failure and data-error rates, all tests green), and
no latched kill switch. Anything missing, malformed, or ambiguous means no live trading.

Hard limits and kill-switch thresholds are in `config/settings.yaml`. The guiding rule is that
**on uncertainty, the system stops trading.**

---

## Methodology

- `Decimal` everywhere in money paths; `float` is rejected at construction and the rejection
  is tested.
- Nothing is labelled `TRUE_ARBITRAGE` without a **payoff-state proof** — enumerate every
  settlement state the relationship permits and show the worst case still clears cost + fees.
- `mutually_exclusive` means **at most one** YES, not exactly one. Exhaustiveness is not
  published by the API and must be proven separately; until then, "buy all YES" baskets are
  rejected as `EXHAUSTIVENESS_UNPROVEN`.
- Statistical arbitrage is never called risk-free.
- Rejected opportunities are recorded with a reason code. Rejection statistics are a primary
  research output, not debug noise.

---

## Known limitations

- The fee model implements the official schedule effective 2026-07-07, but has **not yet been
  reconciled against real fills**. Orders return `taker_fees_dollars` / `maker_fees_dollars`;
  a reconciliation harness is planned for Phase 4. Until then fees are an estimate carrying a
  configurable safety factor. `fee_type=flat` is deliberately **unmodelled and raises**.
- kalshi.com sits behind a bot checkpoint that returns 429 to most automated requests, and the
  Wayback Machine holds only 429s for the fee-schedule URL. Re-verify the schedule manually
  when it matters rather than assuming a fetch will succeed.
- **No public archive of historical order-book depth exists.** Candlesticks and trade prints
  cannot reconstruct a book, so depth-aware backtesting depends on data we record ourselves.
  Capture therefore starts at Phase 4, before the strategies that consume it.
- Basic API tier sustains roughly 20 reads/sec, so a REST-polling scanner cannot cover the
  whole exchange; WebSocket is required for books at scale.
- No strategy has been backtested or paper-traded. Nothing here should be taken as evidence
  that a tradeable edge exists.
