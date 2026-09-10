"""Binary contract mathematics.

A Kalshi market is a binary contract: the YES side pays ``notional`` if the event occurs, the
NO side pays ``notional`` if it does not. Exactly one side pays, so YES and NO are exact
complements.

Two structural facts drive everything here, both established in docs/KALSHI_API_NOTES.md:

1. **The exchange publishes only bids.** Asks are derived from the opposite side's bid. This
   makes single-market YES+NO "arbitrage" algebraically identical to a crossed book, so it is
   modelled here as an *invariant to check*, not an edge to trade (see
   :func:`complement_invariant_holds`).

2. **``mutually_exclusive`` means at most one YES, not exactly one.** Exhaustiveness is not
   published by the API. :class:`Exclusivity` keeps those cases distinct, because they imply
   different guaranteed payoffs and therefore different arbitrage conditions.

This module is pure: no I/O, no Kalshi transport, no configuration.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum, StrEnum

from .types import DEFAULT_NOTIONAL, MoneyError, parse_count, parse_price, to_decimal

__all__ = [
    "Exclusivity",
    "Outcome",
    "PayoffBounds",
    "basket_payoff_bounds",
    "complement_invariant_holds",
    "complement_price",
    "derive_ask",
    "effective_spread",
    "guaranteed_gross_edge",
    "implied_probability",
    "position_payoff",
]


class Outcome(StrEnum):
    """Which side of a binary market a position is on.

    Mirrors the API's canonical ``outcome_side``. Note ``buy YES == sell NO``: there is no
    separate short, so shorting YES *is* buying NO.
    """

    YES = "yes"
    NO = "no"

    @property
    def opposite(self) -> Outcome:
        return Outcome.NO if self is Outcome.YES else Outcome.YES


class Exclusivity(Enum):
    """What an event guarantees about how many of its markets can resolve YES.

    The distinction is load-bearing. ``MUTUALLY_EXCLUSIVE`` is what the API's
    ``Event.mutually_exclusive`` flag actually asserts -- *at most* one YES, permitting zero.
    ``PARTITION`` additionally asserts exhaustiveness, which the API does **not** publish and
    which must be proven separately before any basket relying on it is called arbitrage.
    """

    MUTUALLY_EXCLUSIVE = "mutually_exclusive"  # at most one YES  -> sum P(yes) <= 1
    PARTITION = "partition"  # exactly one YES -> sum P(yes) == 1
    UNCONSTRAINED = "unconstrained"  # any number may resolve YES


@dataclass(frozen=True)
class PayoffBounds:
    """Worst and best settlement payoff of a position, across every permitted state."""

    minimum: Decimal
    maximum: Decimal

    def __post_init__(self) -> None:
        if self.minimum > self.maximum:
            raise MoneyError(f"minimum payoff {self.minimum} exceeds maximum {self.maximum}")

    @property
    def is_certain(self) -> bool:
        """True when the payoff is identical in every state."""
        return self.minimum == self.maximum


def complement_price(
    price: Decimal | int | str, *, notional: Decimal = DEFAULT_NOTIONAL
) -> Decimal:
    """Price of the opposite side: ``notional - price``."""
    return notional - parse_price(price, notional=notional)


def implied_probability(
    price: Decimal | int | str, *, notional: Decimal = DEFAULT_NOTIONAL
) -> Decimal:
    """Risk-neutral implied probability of a price, as a fraction of notional.

    This is the market's price, not a forecast: it embeds the spread, fees the marginal
    trader faced, and any risk premium. Treat it as a quote, never as a calibrated belief.
    """
    if notional <= 0:
        raise MoneyError(f"notional must be positive (got {notional})")
    return parse_price(price, notional=notional) / notional


def position_payoff(
    outcome: Outcome,
    resolved_yes: bool,
    count: Decimal | int | str = 1,
    *,
    notional: Decimal = DEFAULT_NOTIONAL,
) -> Decimal:
    """Settlement payoff of holding ``count`` contracts on ``outcome``.

    Payoff only -- this is gross of what was paid. Note YES and NO payoffs on the same
    resolution always sum to ``notional * count``.
    """
    qty = parse_count(count)
    pays = (outcome is Outcome.YES) == resolved_yes
    return qty * notional if pays else Decimal(0)


def derive_ask(
    opposite_bid: Decimal | int | str, *, notional: Decimal = DEFAULT_NOTIONAL
) -> Decimal:
    """Derive one side's ask from the opposite side's best bid.

    Kalshi publishes bids only: a resting YES bid at $0.60 *is* an offer of NO at $0.40. So
    ``yes_ask = notional - best_no_bid`` and ``no_ask = notional - best_yes_bid``.

    The result is ambiguous at the endpoints -- an ask equal to ``notional`` means either "no
    bids on the other side" or "a bid at zero", and these are not the same thing. Resolve
    against book depth before treating the number as executable.
    """
    return notional - parse_price(opposite_bid, notional=notional, field="opposite_bid")


def complement_invariant_holds(
    yes_bid: Decimal | int | str,
    no_bid: Decimal | int | str,
    *,
    notional: Decimal = DEFAULT_NOTIONAL,
) -> bool:
    """Book-integrity check: ``yes_bid + no_bid <= notional``.

    This is the retired "YES/NO complement arbitrage" seen correctly. Because asks are derived
    from the opposite bid::

        yes_ask + no_ask = 2*notional - (yes_bid + no_bid)
        profit_of_buying_both = notional - (yes_ask + no_ask) = (yes_bid + no_bid) - notional

    so an apparent profit requires ``yes_bid + no_bid > notional`` -- a crossed book, where the
    two orders are each other's counterparty and the matching engine would already have paired
    them. Verified against 1,730 live markets with zero violations.

    A ``False`` here therefore means **bad data**, not free money: a stale cache, a mis-parsed
    side, or no-leg vs yes-leg price scales mixed up (the WebSocket ``use_yes_price`` flag).
    Callers route it to data quality, never to the opportunity table.
    """
    yb = parse_price(yes_bid, notional=notional, field="yes_bid")
    nb = parse_price(no_bid, notional=notional, field="no_bid")
    return yb + nb <= notional


def effective_spread(
    yes_bid: Decimal | int | str,
    no_bid: Decimal | int | str,
    *,
    notional: Decimal = DEFAULT_NOTIONAL,
) -> Decimal:
    """Round-trip spread implied by the two bids: ``notional - (yes_bid + no_bid)``.

    Equals ``yes_ask - yes_bid``. This is the unavoidable cost of crossing both sides of one
    market, and the exact negative of the mythical single-market complement "arbitrage".
    """
    yb = parse_price(yes_bid, notional=notional, field="yes_bid")
    nb = parse_price(no_bid, notional=notional, field="no_bid")
    return notional - (yb + nb)


def basket_payoff_bounds(
    n_legs: int,
    side: Outcome,
    exclusivity: Exclusivity,
    *,
    count: Decimal | int | str = 1,
    notional: Decimal = DEFAULT_NOTIONAL,
) -> PayoffBounds:
    """Worst/best payoff of buying ``count`` contracts on ``side`` of every leg of an event.

    This is the payoff-state proof that gates the ``TRUE_ARBITRAGE`` label: an opportunity
    qualifies only when ``minimum`` payoff already beats total cost plus fees.

    Let ``y`` be the number of legs resolving YES.

    ==================  ========================  ==========================================
    Exclusivity         permitted ``y``           buying every NO leg pays ``(n - y)``
    ==================  ========================  ==========================================
    MUTUALLY_EXCLUSIVE  ``{0, 1}``                ``[n-1, n]``  -- at least ``n-1`` always pay
    PARTITION           ``{1}``                   ``n-1`` exactly
    UNCONSTRAINED       ``[0, n]``                ``[0, n]``    -- no guarantee at all
    ==================  ========================  ==========================================

    The asymmetry is the whole point. Buying every **NO** leg is safe on mutual exclusivity
    alone, because at most one YES means at least ``n-1`` NO legs pay. Buying every **YES**
    leg guarantees nothing unless the event is also exhaustive -- if no outcome occurs the
    entire premium is lost. That is why ``PARTITION`` must be proven, never assumed from the
    API's ``mutually_exclusive`` flag.
    """
    if n_legs < 1:
        raise MoneyError(f"a basket needs at least one leg (got {n_legs})")
    qty = parse_count(count)
    unit = qty * notional
    n = Decimal(n_legs)

    if exclusivity is Exclusivity.PARTITION:
        min_yes = max_yes = Decimal(1)
    elif exclusivity is Exclusivity.MUTUALLY_EXCLUSIVE:
        min_yes, max_yes = Decimal(0), Decimal(1)
    else:
        min_yes, max_yes = Decimal(0), n

    if side is Outcome.YES:
        # YES legs pay on the legs that resolve YES.
        return PayoffBounds(minimum=min_yes * unit, maximum=max_yes * unit)
    # NO legs pay on every leg that does *not* resolve YES; fewer YES resolutions pays more.
    return PayoffBounds(minimum=(n - max_yes) * unit, maximum=(n - min_yes) * unit)


def guaranteed_gross_edge(total_cost: Decimal | int | str, bounds: PayoffBounds) -> Decimal:
    """Worst-case gross edge: ``minimum payoff - total cost``, before fees.

    Deliberately uses the *minimum* payoff. An arbitrage claim must survive the worst
    permitted settlement state, not the average or the hoped-for one.
    """
    return bounds.minimum - to_decimal(total_cost, field="total_cost")
