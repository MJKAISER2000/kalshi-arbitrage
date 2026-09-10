# Kalshi API — Research Notes

**Researched:** 2026-09-09. **Method:** official docs (`docs.kalshi.com`), the machine-readable
OpenAPI/AsyncAPI specs, the CFTC-filed fee schedule, and **direct calls against the live
production API**. Every claim below is tagged:

- **[VERIFIED-LIVE]** — confirmed by querying the production API during this research.
- **[DOC]** — stated in current official documentation.
- **[FILED]** — from a CFTC regulatory filing (authoritative but may predate the current schedule).
- **[UNVERIFIED]** — could not be confirmed; the code must not silently assume it.

> Source of truth for the code is the API itself. Where a value is per-market or per-series
> (fees, tick size, notional), we read it from the API at runtime and never hard-code it.

---

## 1. Environments and endpoints

| Environment | REST base | WebSocket |
|---|---|---|
| Production | `https://external-api.kalshi.com/trade-api/v2` | `wss://external-api-ws.kalshi.com/trade-api/ws/v2` |
| Demo | `https://external-api.demo.kalshi.co/trade-api/v2` | `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2` |

Legacy hosts (`api.elections.kalshi.com`, `demo-api.kalshi.co`) still work. The `external-api`
hosts are the recommended ones for API traders. **[DOC]**

Credentials are **not** shared between environments. Demo keys only work against demo. **[DOC]**

Public market-data endpoints require **no authentication** — confirmed by fetching
`/exchange/status`, `/markets`, `/events`, and `/markets/{ticker}/orderbook` unauthenticated.
**[VERIFIED-LIVE]**

### Exchange shards

`/exchange/status` returns `exchange_index_statuses`. Live today: `0` Default, `1` Combos,
`2` Crypto, `3` Tennis & Baseball. Each shard reports `trading_active` independently — a
scanner must check the *shard* status, not just the global flag. **[VERIFIED-LIVE]**

---

## 2. Authentication

Three headers on every private request **[DOC]**:

| Header | Value |
|---|---|
| `KALSHI-ACCESS-KEY` | API key ID |
| `KALSHI-ACCESS-TIMESTAMP` | current time in **milliseconds** |
| `KALSHI-ACCESS-SIGNATURE` | Base64 RSA-PSS/SHA-256 signature |

Signed message = `timestamp + UPPERCASE_METHOD + path`, e.g.
`1703123456789GET/trade-api/v2/portfolio/balance`.

**Sign the full path from the API root, without the query string and without the host.** The
host does not change the signature payload. **[DOC]**

Keys live in environment variables only (`KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH`) and
the private key file is never committed — see `.gitignore` and `.env.example`.

---

## 3. Price and quantity representation — fixed-point strings, not integers

This is the single most important correctness constraint in the API.

- **Prices**: fixed-point **dollar strings**, suffix `_dollars`, up to **4 decimal places**
  (`"0.4200"`). Sub-cent prices exist. Legacy integer-cent fields cannot represent them —
  on sub-cent markets you *must* read the `_dollars` fields. **[DOC]**
- **Quantities**: fixed-point strings, suffix `_fp`, up to **2 decimal places** (`"13.00"`).
  **Fractional contracts are real**; minimum granularity is 0.01 contracts. **[DOC]**
- Intermediate fee math can reach **6 decimal places**. **[DOC]**

**Engineering consequence:** the entire codebase uses `decimal.Decimal`. Binary floats are
banned in money paths. We parse straight from the API's strings and never round-trip through
`float`. This is enforced by tests.

### Tick grid is per-market and dynamic

Each market carries `price_ranges` — an array of `{start, end, step}` bands — and a
human-readable `price_level_structure` label. **`price_ranges` is the source of truth**;
off-grid prices are rejected. The docs explicitly warn *not* to key logic off the structure
name because new structures are added over time. **[DOC]**

Observed live: `price_level_structure: "center_deci_edge_centi_cent"` with
`price_ranges: [{0.0000-0.0100, step 0.0001}, {0.0100-0.9900, step 0.0010}, ...]`.
**[VERIFIED-LIVE]** — note this market's tick is *not* one cent. Tick size must never be
assumed to be `$0.01`.

### Notional is a field, not a constant

`notional_value_dollars` is present on every market (observed `"1.0000"`). Payoff math reads
this field rather than assuming a $1 contract. **[VERIFIED-LIVE]**

---

## 4. Order book semantics — bids only (critical)

`GET /markets/{ticker}/orderbook` returns `orderbook_fp` with two arrays, `yes_dollars` and
`no_dollars`. Each level is `[price_dollars, count_fp]`. Arrays are sorted **ascending**, so
the **best bid is the last element**. **[DOC]** **[VERIFIED-LIVE]**

**Kalshi publishes only bids.** There are no ask arrays. Asks are *derived*:

```
yes_ask = notional - best_no_bid
no_ask  = notional - best_yes_bid
```

Because a YES bid at $0.60 *is* a NO ask at $0.40 — the same resting order seen from the
other side.

### Consequence: single-market YES+NO "arbitrage" is an identity, not an opportunity

Buying both legs of one market costs `yes_ask + no_ask` and pays exactly `notional`:

```
yes_ask + no_ask = (1 - no_bid) + (1 - yes_bid) = 2 - (yes_bid + no_bid)
profit           = 1 - (yes_ask + no_ask) = (yes_bid + no_bid) - 1 = -spread
```

So the "YES + NO < $1" condition is *algebraically equivalent to a crossed book*
(`yes_bid + no_bid > $1`), which the matching engine prevents — those two orders are each
other's counterparty and would have matched.

**Empirically tested on 1,730 live markets: the identity held exactly 1,730/1,730 times, with
zero crossed books.** **[VERIFIED-LIVE]**

This retires the naive Strategy #1 as a source of edge. See `STRATEGIES.md` §1 — it is
retained in the system only as a **book-integrity invariant check**, where a violation means
stale data or a bug, not profit.

### Zero and one are ambiguous — do not treat them as prices

An empty NO book yields `yes_ask = notional - 0 = $1.00`; a NO bid at $0.00 yields the same
number. Likewise `yes_ask = "0.0000"` means a NO bid at $1.00, not a free contract. A live
provisional market was observed quoting `yes_bid=0, yes_ask=0, no_bid=1, no_ask=1`.
**[VERIFIED-LIVE]** Top-of-book summary fields therefore cannot express "no liquidity" — the
book (with sizes) must be consulted. This is a designed-in false-arbitrage trap.

### WebSocket price scale differs by default

On `orderbook_delta` / `orderbook_snapshot`, **no-side levels are reported in no-leg pricing
by default** — a no-side level at `0.30` means "NO at 30c" (matching "YES at 70c"). Passing
`use_yes_price: true` in the subscribe params puts both sides on the yes scale. The default
will flip to `true` in a future release and the flag will later be removed. **[DOC]**

**Our client sets `use_yes_price: true` explicitly** so the meaning is stable across that
migration, and records the flag alongside stored data.

---

## 5. Order direction

Canonical fields are `outcome_side` (`yes`|`no`) and `book_side` (`bid`|`ask`), where
**`bid == yes` and `ask == no`, always**. Public trades use `taker_outcome_side` /
`taker_book_side`. **[DOC]**

Direction does **not** change price: an order at price `p` with `outcome_side=no` matches an
order at the same `p` with `outcome_side=yes`.

Legacy `action`/`side`/`is_yes`/`purchased_side`/`taker_side` are deprecated. Dated removal
notices appear in the spec (`will not be removed before May 2026` — i.e. already past).
**New code reads only `outcome_side`/`book_side`.**

Equivalences that matter for position keeping: **buy-yes == sell-no**, and **buy-no == sell-yes**.
There is no separate "short"; shorting YES *is* buying NO.

---

## 6. Fees

### Formulas **[DOC]** (official Kalshi Fee Schedule, effective 2026-07-07)

Retrieved from `kalshi.com/docs/kalshi-fee-schedule.pdf`. The site sits behind a bot
checkpoint that returns 429 to most automated requests -- it took several attempts, and the
Wayback Machine holds only 429 responses for this URL, so re-verify manually when it matters.

```
taker fee = round_up( M_taker * 0.07   * C * P * (1 - P) )     M_taker default 1
maker fee = round_up( M_maker * 0.0175 * C * P * (1 - P) )     M_maker default 0

  C = number of contracts
  P = price per contract in dollars
  M = per-series multiplier (NOT the rate)
```

**`M` is a multiplier on the base rate, not the rate itself.** The API's `fee_multiplier`
field carries `M`. Wiring it in as the rate overstates fees ~14x. The code funnels API values
through `FeeSchedule.from_api()` for exactly this reason, and a regression test pins it.

Note `0.0175 = 0.25 x 0.07`, so a maker at `M_maker = 1` pays a quarter of the taker rate and
at `M_maker = 2` pays half -- which is precisely what the `fee_type` names encode.

**Rounding:** the schedule states round-up brings *fee plus position cost* to a **centicent**
(`$0.0001`). The Fee Rounding doc adds that direct members align to `$0.0001` and FCM-cleared
members to `$0.01`. The published General Trading Fees Table is cent-rounded (100 @ $0.35 ->
$1.5925 -> $1.60), consistent with the non-direct case. Both are supported via
`MemberPrecision`; the default is the coarser `$0.01`, since overstating fees only costs
marginal trades while understating them manufactures arbitrage that is not there.

The round-up applies to the **order total**, not per contract -- confirmed by the table
($1.5925 -> $1.60, not 100 x ceil($0.015925) = $2.00).

### Per-series multipliers **[DOC]**

The schedule's "Non-Standard Fees" table lists maker and taker `M` per series. Values observed
live are `0` and `1`. **`M` varies by series and some series carry `M = 0`**, so the multiplier
must be read per series at runtime and never assumed.

`fee_type` maps to the maker `M`: `quadratic` -> 0, `quadratic_with_maker_fees` -> 1,
`quadratic_with_combo_maker_fees` -> 2. Event-level `fee_type_override` /
`fee_multiplier_override` layer on top, and scheduled changes are exposed via
`GET /series/fee_changes` and `GET /events/fee_changes` — a multiplier can change under a
running strategy, so re-check it before entry.

### Other fees **[DOC]**

**No settlement fee, no membership fee, no ACH deposit or withdrawal fee.** (The $2 ACH
withdrawal fee in the 2022 filing has been removed.) Card deposits up to 2%; wire withdrawals
only above $500,000; alternate rails 0-2% at Kalshi's discretion. Perpetual futures use an
entirely separate bps-based tiered schedule, out of scope here.

### What remains UNVERIFIED

`fee_type=flat` -- the current schedule expresses non-standard pricing through per-series
multipliers, with no flat per-contract table, so the code leaves `FLAT` **unmodelled and
raises** rather than guessing. Orders and fills return `taker_fees_dollars` and
`maker_fees_dollars`, so Phase 4 adds a reconciliation harness comparing modeled to actual
fees. Until reconciled the model is an *estimate with a safety margin*, not ground truth.

---

## 7. Events, markets, and logical structure

Hierarchy: **Series** → **Event** → **Market**. A market is one binary YES/NO contract.

### `mutually_exclusive` — and the exhaustiveness gap

`Event.mutually_exclusive: bool` — *"If true, only one market in this event can resolve to
'yes'."* **[DOC]**

Read that precisely: **at most one**, not exactly one. The API does **not** publish
exhaustiveness. So for a mutually-exclusive event with N markets:

```
sum P(yes_i) <= 1        (guaranteed by mutual exclusivity)
sum P(yes_i) =  1        only if ALSO exhaustive — NOT given by the API
```

This asymmetry decides which side of a basket is a real arbitrage:

| Trade | Payoff | Arbitrage condition | Needs exhaustiveness? |
|---|---|---|---|
| Buy **all NO** legs | >= N-1 guaranteed | `sum no_ask < N-1`  ==  `sum yes_bid > 1` | **No** — safe on mutual exclusivity alone |
| Buy **all YES** legs | <= 1, could be **0** | `sum yes_ask < 1` | **Yes** — otherwise not an arbitrage at all |

Buying every YES when the event is *not* exhaustive risks a total loss of premium (no outcome
occurs). Treating "underround" as arbitrage without proving exhaustiveness is a headline
false-arbitrage class, tracked explicitly as `EXHAUSTIVENESS_UNPROVEN`.

Live sample of 200 open events: **36 mutually exclusive, 164 not** — so the flag genuinely
discriminates and must be checked per event. **[VERIFIED-LIVE]**

### Structured strikes give machine-readable logic — no NLP needed

Markets carry `strike_type` in `greater` | `greater_or_equal` | `less` | `less_or_equal` |
`between` | `functional` | `custom` | `structured`, plus `floor_strike`, `cap_strike`,
`functional_strike`, `custom_strike`. **[DOC]** **[VERIFIED-LIVE]**

For markets in one event over a common underlying, these yield **exact** logical relations —
e.g. `P(X >= k)` is non-increasing in `k`, so a strike ladder must be monotone. This is a
rigorous basis for logical arbitrage (Strategy #4) that requires no embeddings and carries no
semantic-similarity risk. Embeddings are demoted to *candidate generation only*.

### Other fields that matter

`Market`: `ticker`, `event_ticker`, `market_type`, `status`, `notional_value_dollars`,
`yes_bid_dollars`, `yes_ask_dollars`, `no_bid_dollars`, `no_ask_dollars`, `yes_bid_size_fp`,
`yes_ask_size_fp`, `last_price_dollars`, `volume_fp`, `volume_24h_fp`, `open_interest_fp`,
`liquidity_dollars`, `result`, `can_close_early`, `rules_primary`, `rules_secondary`,
`price_ranges`, `price_level_structure`, `settlement_timer_seconds`, `exchange_index`.
**[VERIFIED-LIVE]**

`Event`: `event_ticker`, `series_ticker`, `title`, `sub_title`, `mutually_exclusive`,
`collateral_return_type`, `settlement_sources`, `strike_date`/`strike_period`.

Multivariate (combo) events use the `KXMVE*` prefix, carry `mve_collection_ticker` and
`mve_selected_legs`, live on the Combos shard, and can be `is_provisional`.
**[VERIFIED-LIVE]**

---

## 8. Market lifecycle **[DOC]**

`initialized -> active -> (inactive) -> closed -> determined -> (disputed -> amended) -> finalized`

Filter values on `GET /markets?status=` map differently from the returned statuses:
`unopened`→`initialized`, `open`→`active`, `paused`→`inactive`, `closed`→ anything past
close not yet finalized, `settled`→`finalized`.

Trading-relevant behaviours:

- **Reactivation cancels all resting orders** (`inactive -> active`).
- After `close_time`, *all* order operations **including cancels** are rejected with
  `MARKET_INACTIVE`; resting orders are cancelled shortly after.
- `close_time` can move — earlier (if `can_close_early`) or later (reopening a closed market).
- `expected_expiration_time` may be **before** `close_time`.
- A market can 404 immediately after its `created` event; retry with backoff.

Settlement: YES holders get `notional` per contract if the result is `yes`, NO holders if
`no`. Only **net** positions settle (after netting). A `settlement_timer_seconds` window runs
at `determined` during which the result may be disputed. Settlement fees are zero for simple
yes/no determination but may apply to sub-cent scalar settlement. **[DOC]**

**Legging-risk consequence:** a market pausing, closing early, or being disputed mid-basket
is a concrete way a "risk-free" basket becomes an unhedged directional position.

---

## 9. Rate limits **[DOC]**

Token-bucket, **two independent budgets**: **Read** (GETs) and **Write** (order
placement/amend/cancel, order groups, RFQ quote flow). Default cost **10 tokens** per request;
`GET /account/endpoint_costs` is the authoritative list of non-default costs.

| Tier | Read tok/s | Write tok/s |
|---|---|---|
| Basic | 200 | 100 |
| Advanced | 300 | 300 |
| Expert | 600 | 600 |
| Premier | 1,000 | 1,000 |
| Paragon | 2,000 | 2,000 |
| Prime | 4,000 | 4,000 |
| Prestige | 10,000 | 8,000 |

At Basic that is **~20 reads/sec and ~10 order writes/sec**. Buckets above Basic hold two
seconds of budget, allowing a 2x burst.

**Batching does not save tokens** — 25 orders in a batch costs 25x the per-order cost, and the
whole batch is rejected unless the full amount is available at once.

429 returns `{"error": "too many requests"}` with **no `Retry-After` and no `X-RateLimit-*`
headers**, and no cooldown penalty. Use exponential backoff.

**Design consequence:** a REST-polling scanner cannot cover thousands of markets at Basic
tier. WebSocket for books, REST for metadata and reconciliation.

---

## 10. WebSocket **[DOC]**

`wss://external-api-ws.kalshi.com/trade-api/ws/v2`, same RSA-PSS auth. Channels relevant here:
`orderbook_delta` / `orderbook_snapshot`, `ticker`, `trade` (public), `fill` (user),
`market_positions`, `market_lifecycle_v2` (all non-MVE markets),
`multivariate_market_lifecycle` (`KXMVE*`).

`market_lifecycle_v2` events: `created`, `activated`, `deactivated`, `close_date_updated`,
`determined`, `settled`, `metadata_updated`, plus `event_fee_update` when an event-level fee
override changes, and `price_level_structure_updated` carrying new `price_ranges`.

Note `initialized -> active` is **time-implicit and emits no event** — a poller is still
required to notice markets opening.

---

## 11. Historical data **[DOC]**

Candlesticks (`GET /series/{s}/markets/{t}/candlesticks`, batch and event variants), public
trades, and an archived historical set (`/historical/*`: markets, trades, fills, orders,
positions, plus `GET /historical/cutoff_timestamps`).

**There is no public archive of historical order-book depth.** Candlesticks and trade prints
do not reconstruct the book. Any backtest that needs depth must use **data we record
ourselves** — which is why data capture starts early (Phase 4) rather than after the
strategies are written.

---

## 12. Live-data observations

Measured 2026-09-09 against production. These record **exchange behaviour**; measured trading
opportunities are kept in private research notes.

1. **YES/NO identity holds 1,730/1,730** with no crossed books, so single-market complement
   arbitrage is structurally impossible. *(section 4)*
2. **Fees round up per order**, so a multi-leg basket pays one round-up per leg while its edge
   does not scale with leg count. Any basket strategy must model this explicitly; it commonly
   dominates the gross edge.
3. **164 of 200 open events are not mutually exclusive** — the flag must be read per event,
   and even when set it proves only *at most one* YES.
4. Tick size is genuinely sub-cent on some markets (`step: "0.0001"`).
5. Series fee multipliers observed live as `0` and `1`, confirming `fee_multiplier` is `M` and
   not a rate.
6. The API returns UTF-8; on Windows, `json.load` on a file opened with the platform default
   (cp1252) **crashes** on live payloads. All file I/O in this project pins `encoding="utf-8"`.

---

## 13. Rules the implementation follows

1. `Decimal` everywhere in money paths; parse from the API's fixed-point strings.
2. Never assume tick size, notional, or fee rate — read `price_ranges`,
   `notional_value_dollars`, `fee_type`/`fee_multiplier` from the API.
3. Never derive executable size from top-of-book alone; walk the book.
4. Treat `ask == 0` / `ask == notional` as *possibly no liquidity*, never as a price.
5. Read only `outcome_side` / `book_side`.
6. Subscribe with `use_yes_price: true` and record that fact with the data.
7. `mutually_exclusive` proves *at most one*; exhaustiveness needs separate proof.
8. `fee_multiplier` from the API is `M`, never a rate; build schedules via `FeeSchedule.from_api()`.
9. Fee model is an estimate until reconciled against `taker_fees_dollars` / `maker_fees_dollars`.
10. Fail loudly on unmodelled cases (e.g. `fee_type=flat`) rather than guessing.

---

## 14. Sources

- API docs index: <https://docs.kalshi.com/llms.txt>
- Environments: <https://docs.kalshi.com/getting_started/api_environments>
- Auth: <https://docs.kalshi.com/getting_started/quick_start_authenticated_requests>
- Order book: <https://docs.kalshi.com/getting_started/orderbook_responses>
- Order direction: <https://docs.kalshi.com/getting_started/order_direction>
- Fixed point: <https://docs.kalshi.com/getting_started/fixed_point_migration>
- Fee rounding: <https://docs.kalshi.com/getting_started/fee_rounding>
- Rate limits: <https://docs.kalshi.com/getting_started/rate_limits>
- Lifecycle: <https://docs.kalshi.com/getting_started/market_lifecycle>
- Settlement: <https://docs.kalshi.com/getting_started/market_settlement>
- Historical: <https://docs.kalshi.com/getting_started/historical_data>
- OpenAPI: <https://docs.kalshi.com/openapi.yaml> · AsyncAPI: <https://docs.kalshi.com/asyncapi.yaml>
- Fee schedule (current, effective 2026-07-07): <https://kalshi.com/docs/kalshi-fee-schedule.pdf>
- Fee schedule (CFTC filing, 2022-09-22, superseded): <https://www.cftc.gov/sites/default/files/filings/orgrules/22/09/rule091222kexdcm003.pdf>
