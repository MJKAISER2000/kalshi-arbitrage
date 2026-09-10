# Architecture

## Principle

The system exists to answer one question:

> After fees, liquidity, latency, partial fills, legging risk, market impact and capital
> constraints, does an economically meaningful and repeatable edge actually exist?

Everything is built to make a **negative** answer just as easy to reach and just as
well-evidenced as a positive one. The rejection path is as instrumented as the execution path.

## Dependency direction

Layers depend only downward. The money math at the bottom has no I/O and no knowledge of
Kalshi transport, so it is exhaustively testable in isolation.

```
                    cli / dashboard
                          |
        +-----------------+-----------------+
        |                 |                 |
    backtest           paper              live            <- run modes
        |                 |                 |
        +--------+--------+--------+--------+
                 |                 |
             portfolio           risk                     <- capital + limits
                 |                 |
                 +--------+--------+
                          |
                     strategies                           <- BaseStrategy implementations
                          |
        +-----------------+-----------------+
        |                 |                 |
    arbitrage        execution         relationships       <- detection / simulation / logic
        |                 |                 |
        +--------+--------+--------+--------+
                 |                 |
             orderbook           markets                   <- book reconstruction, normalization
                 |                 |
                 +--------+--------+
                          |
              +-----------+-----------+
              |                       |
          contracts                 fees                   <- PURE MONEY MATH (no I/O)
              |                       |
              +-----------+-----------+
                          |
                        types                              <- Decimal money primitives
                          |
        +-----------------+-----------------+
        |                 |                 |
       api              data              utils            <- transport, storage, logging
```

## Package map

| Package | Responsibility | Status |
|---|---|---|
| `types` | `Price`, `Quantity`, `Money` — Decimal-based, validated | **implemented** |
| `contracts` | Binary payoff math, complements, implied probability, basket payoff bounds | **implemented** |
| `fees` | `FeeModel` driven by the API's `fee_type` / `fee_multiplier` | **implemented** |
| `config` | Settings loading and the trading-mode gate | **implemented** |
| `api` | REST client, RSA-PSS auth, tier-aware rate-limit budgets, retries | **implemented** (WebSocket: Phase 5) |
| `markets` | Normalized market/event/series models, strike parsing | **implemented** (relationships: Phase 10) |
| `data` | Snapshot capture, SQLite time-series storage, ingest audit | **implemented** |
| `orderbook` | Book reconstruction, VWAP, execution price, depth, impact | Phase 5 |
| `arbitrage` | `ArbitrageOpportunity`, `RejectedOpportunity`, scanners | Phase 6 |
| `execution` | Fill simulation, partial fills, leg risk, latency stress | Phase 7 |
| `strategies` | `BaseStrategy` registry | Phase 6+ |
| `portfolio` / `risk` | Positions, P&L, exposure, hard limits, kill switch | Phase 11 |
| `backtest` | Event-driven, chronological, no lookahead | Phase 8 |
| `paper` / `live` | Run modes; `live` gated shut | Phase 11 / 17 |
| `analytics` | Attribution, capacity, decay, reports | Phase 14 |

Directories for later phases exist but are **empty** — no speculative abstractions. A package
gets code when its phase starts.

## Money representation

`decimal.Decimal` everywhere in money paths; `float` is banned and the ban is tested. Values
are parsed directly from the API's fixed-point strings (`_dollars`, `_fp`).

Three distinct quantities, deliberately not interchangeable:

- `Price` — per contract, `0 <= p <= notional`, up to 4dp.
- `Quantity` — contracts, up to 2dp, fractional contracts are real.
- `Money` — dollar amounts, 6dp internally (fee math needs it).

The tick grid comes from each market's `price_ranges`, never assumed to be one cent. Notional
comes from `notional_value_dollars`, never assumed to be $1.

## Trading-mode gate

Live trading must be impossible by accident. It requires **all** of:

1. `TRADING_MODE=live`
2. `ENABLE_LIVE_TRADING=true`
3. a live-gate check on paper-trading record (§53 of the brief)
4. no kill switch latched

Default is `TRADING_MODE=paper`, `ENABLE_LIVE_TRADING=false`. The gate **fails closed** on
missing, malformed, or ambiguous configuration. Every other component receives an explicit
mode object rather than reading globals, so an execution path cannot reach the live client
without having been handed it.

## Opportunity lifecycle

```
market data -> book reconstruction -> relationship layer
     -> candidate generation -> payoff-state proof -> fee model
     -> liquidity walk -> leg-risk model -> latency stress
     -> risk manager -> capital allocator -> (paper|live) execution
```

A candidate rejected at any stage becomes a `RejectedOpportunity` with a reason code, and is
persisted. Rejection statistics are a primary research output, not debug noise.

## Testing posture

- Every financial calculation has unit tests, including boundary and adversarial cases.
- Property-based tests assert invariants (payoff consistency, basket bounds, liquidity caps).
- Live-data conformance tests assert the API still behaves as `KALSHI_API_NOTES.md` claims,
  so an upstream change fails a test rather than silently corrupting P&L.
- The fee model gets a reconciliation harness against real `taker_fees_dollars` /
  `maker_fees_dollars` once authenticated trading data exists.
