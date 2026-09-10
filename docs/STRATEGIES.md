# Strategies

Every strategy carries a **class label**. These are not interchangeable and the system never
blurs them.

| Class | Meaning | Risk if correctly identified |
|---|---|---|
| `TRUE_ARBITRAGE` | Payoff dominates cost in **every** settlement state, given the legs fill | Execution risk only (legging, partial fill, market pause) |
| `STATISTICAL_ARBITRAGE` | Positive expectancy from a historical relationship | Model risk + execution risk. **Never** called risk-free |
| `RELATIVE_VALUE` | Mispricing vs. a related instrument, no settlement guarantee | Directional risk remains |
| `SPECULATIVE` | Directional view | Full directional risk |

A `TRUE_ARBITRAGE` label requires a **payoff-state proof**: enumerate every settlement state
the relationship permits and show `min(payoff) - cost - fees > 0`. Nothing is labelled
`TRUE_ARBITRAGE` on price comparison alone.

---

## 1. Single-market YES/NO complement — **RETIRED as a strategy**

**Original hypothesis:** find `yes_ask + no_ask + fees < $1`.

**Finding: this is structurally impossible on Kalshi.** The exchange publishes only bids;
asks are derived as `yes_ask = notional - no_bid` and `no_ask = notional - yes_bid`. So

```
profit = notional - (yes_ask + no_ask) = (yes_bid + no_bid) - notional = -spread
```

The condition is algebraically identical to a **crossed book**, which the matching engine
prevents — the two orders are each other's counterparty. Verified on 1,730 live markets:
identity held 1,730/1,730, zero crossings. See `KALSHI_API_NOTES.md` §4.

**What it becomes instead:** a **book-integrity invariant**. `BinaryComplementInvariant`
asserts `yes_bid + no_bid <= notional` on every book we ingest. A violation is a **data
quality alarm** (stale cache, mis-parsed side, wrong price scale from the `use_yes_price`
flag, crossed feed) — not a trade. It fires into the data-quality system, never the
opportunity table.

This is the project's first concrete result: it removes a strategy most naive Kalshi bots
implement first, and converts it into a bug detector.

---

## 2. Multi-outcome basket (mutually exclusive events) — **primary TRUE_ARBITRAGE candidate**

The API exposes `Event.mutually_exclusive`, meaning **at most one** market resolves YES. It
does **not** expose exhaustiveness. That asymmetry determines which direction is a real
arbitrage.

Let an event have `N` markets, notional `V`.

### 2a. Overround / "buy all NO" — safe on mutual exclusivity alone

At most one YES means **at least `N-1` NO legs pay out**. Payoff is `>= (N-1)*V`, so:

```
cost      = sum(no_ask_i)  = sum(V - yes_bid_i) = N*V - sum(yes_bid_i)
guaranteed payoff >= (N-1)*V
gross edge >= sum(yes_bid_i) - V
```

**Condition:** `sum(yes_bid_i) > V`. Requires no exhaustiveness assumption. The upside is
asymmetric in our favour — if *no* outcome resolves YES, all `N` legs pay and we earn more.

### 2b. Underround / "buy all YES" — requires proven exhaustiveness

```
cost = sum(yes_ask_i),  payoff = V if some outcome occurs, else 0
```

`sum(yes_ask_i) < V` is **only** an arbitrage if the event is exhaustive. If it is not, the
worst case is losing the entire premium. The API does not tell us. Until an
`ExhaustivenessProof` exists for the event, such a signal is rejected with
`EXHAUSTIVENESS_UNPROVEN` and, if traded at all, is classed `SPECULATIVE`, never
`TRUE_ARBITRAGE`.

Exhaustiveness may be established by: an explicit "none of the above" market completing the
partition; `rules_primary` text stating the partition (reviewed, then pinned in
`config/markets.yaml` with provenance); or a settled-history check that some outcome always
resolved YES. All three are recorded with a confidence and a source.

### Sizing

`Q_max = min_i(executable_quantity_i)` — walking each book, never top-of-book size alone. The
basket is capped by its **weakest leg**, and each additional leg is another chance for the
minimum to be small.

### Why this is hard here

Fees are charged **per order** and rounded up independently, so an N-leg basket pays N
round-ups while the gross edge does not scale with N. The fee is also quadratic in price,
peaking at mid-market. Against live multi-outcome events this routinely converts a real gross
overround into a net loss.

Two consequences for the design:

- The fee model is a **gate** evaluated before an opportunity is reported, not a column in the
  output. Leg count is a first-class cost driver, and `max_leg_count` is a configured limit.
- Capital lockup matters as much as edge. A basket held to settlement ties up capital until the
  event resolves, so `net_edge_per_dollar_per_day` is the ranking metric, not headline edge.

Measured examples, and the specific conditions under which basket edge does survive costs, are
kept in private research notes rather than published here.

---

## 3. Logical arbitrage from structured strikes — **highest-value untapped area**

Markets carry `strike_type` (`greater`, `greater_or_equal`, `less`, `less_or_equal`,
`between`, `functional`, `custom`, `structured`) with `floor_strike` / `cap_strike`. For
markets over a common underlying this yields **exact, derivable** logical relations — no NLP,
no embedding similarity, no semantic guesswork.

Canonical case, a **strike ladder**: markets on `X >= k` for increasing `k` must satisfy

```
P(X >= k1) >= P(X >= k2)   for k1 < k2
```

A violation is a genuine arbitrage: buy the cheap-but-more-likely leg, sell the
dear-but-less-likely one. The two legs' payoffs dominate state-by-state.

More generally, from a relation we derive a **price constraint** and check executable prices
against it:

| Relation | Constraint |
|---|---|
| `A implies B` | `P(A) <= P(B)` |
| `A excludes B` | `P(A) + P(B) <= 1` |
| `A equivalent B` | `P(A) = P(B)` |
| `A complement B` | `P(A) + P(B) = 1` |
| `A subset-of B` | `P(A) <= P(B)` |

Every relation carries `type`, a formal definition, `confidence`, `source`
(`derived_from_strikes` | `curated` | `inferred`), and validation rules. **Only
`derived_from_strikes` and `curated` relations may produce a `TRUE_ARBITRAGE` label.**
Inferred (embedding/NLP) relations generate *candidates* for review and are capped at
`RELATIVE_VALUE`.

---

## 4. Temporal / calendar structure

For a cumulative event, `P(by T1) <= P(by T2)` when `T1 < T2`. Same machinery as §3 with the
relation derived from `strike_date` / `strike_period` / `close_time`.

**Danger:** two markets that look like the same event at different horizons frequently have
different settlement sources or rules. A temporal relation is only accepted when the pair
shares a series and settlement definition, or has been curated. Otherwise it is
`RELATIVE_VALUE` at best.

---

## 5. Cross-market relative value — `STATISTICAL_ARBITRAGE`, never risk-free

Convert prices to implied probabilities and compare related markets, clusters, and historical
relationships (spread, z-score, percentile, mean reversion, correlation). Requires the
historical data captured from Phase 4 onward.

Labelled `STATISTICAL_ARBITRAGE` or `RELATIVE_VALUE`. Never `TRUE_ARBITRAGE`, regardless of
how strong the historical relationship looks.

---

## Rejection taxonomy

Every candidate that fails is recorded as a `RejectedOpportunity` with a reason. Rejection
statistics are a primary research output — they show *where* naive arbitrage dies.

`FEES_EXCEED_EDGE`, `INSUFFICIENT_LIQUIDITY`, `EXHAUSTIVENESS_UNPROVEN`,
`RELATIONSHIP_UNPROVEN`, `SETTLEMENT_MISMATCH`, `EXPIRATION_MISMATCH`, `STALE_QUOTE`,
`MARKET_NOT_ACTIVE`, `SHARD_INACTIVE`, `TICK_GRID_VIOLATION`, `LEG_RISK_DOMINATES`,
`LATENCY_FRAGILE`, `CAPITAL_LOCKUP_TOO_LONG`, `BELOW_MIN_EDGE`, `POSITION_LIMIT`,
`INSUFFICIENT_CAPITAL`, `PRICE_MOVED`, `CORRELATED_NOT_IDENTICAL`, `AMBIGUOUS_ASK`
(ask at 0 or notional with no depth behind it).

---

## Priority

1. **Multi-outcome basket (§2a)** — provable from published event structure, measurable today.
2. **Logical arbitrage from strikes (§3)** — exact relations already in the API, and few legs,
   which keeps fee drag low.
3. Exhaustiveness proofs (unlocks §2b).
4. Temporal (§4).
5. Statistical relative value (§5) — only after data collection.

§1 is done: retired and repurposed as an invariant.

Screening thresholds, series selection, and measured edge statistics live in private research
notes. This document covers the **method**; it deliberately does not publish which markets the
method currently favours.
