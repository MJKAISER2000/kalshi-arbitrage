"""Normalized market, event, and series models.

These parse the API's fixed-point strings into ``Decimal`` at the boundary, so nothing
downstream ever sees a raw payload or a float. Unknown fields are ignored rather than
rejected: Kalshi adds fields regularly, and a scanner that dies on a new one is worse than a
scanner that ignores it.

The subtle part is :attr:`NormalizedMarket.yes_ask` and friends. Kalshi publishes only bids
and derives asks from the opposite side, so an ask at ``0`` or at ``notional`` is
**ambiguous** -- it can mean "no liquidity on the other side" rather than a real price. The
model exposes that ambiguity explicitly instead of letting it read as a tradeable quote.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..contracts import Exclusivity

__all__ = [
    "MarketStatus",
    "NormalizedEvent",
    "NormalizedMarket",
    "NormalizedSeries",
    "PriceBand",
    "StrikeType",
]


class MarketStatus(StrEnum):
    """Lifecycle states returned by the REST API."""

    INITIALIZED = "initialized"
    ACTIVE = "active"
    INACTIVE = "inactive"
    CLOSED = "closed"
    DETERMINED = "determined"
    DISPUTED = "disputed"
    AMENDED = "amended"
    FINALIZED = "finalized"

    @property
    def is_tradeable(self) -> bool:
        """Only ``active`` accepts orders.

        ``inactive`` is a pause, not a close -- and reactivation cancels every resting order,
        so a paused market is a live legging risk, not merely a gap in the data.
        """
        return self is MarketStatus.ACTIVE


class StrikeType(StrEnum):
    """How a market's strike is defined. The basis for derived logical relations."""

    GREATER = "greater"
    GREATER_OR_EQUAL = "greater_or_equal"
    LESS = "less"
    LESS_OR_EQUAL = "less_or_equal"
    BETWEEN = "between"
    FUNCTIONAL = "functional"
    CUSTOM = "custom"
    STRUCTURED = "structured"

    @property
    def is_monotone_threshold(self) -> bool:
        """True for one-sided thresholds, where a strike ladder must be monotone in price."""
        return self in {
            StrikeType.GREATER,
            StrikeType.GREATER_OR_EQUAL,
            StrikeType.LESS,
            StrikeType.LESS_OR_EQUAL,
        }


class PriceBand(BaseModel):
    """One band of a market's tick grid, from ``price_ranges``."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    start: Decimal
    end: Decimal
    step: Decimal

    def contains(self, price: Decimal) -> bool:
        return self.start <= price <= self.end

    def is_on_grid(self, price: Decimal) -> bool:
        """Whether ``price`` lands exactly on this band's tick grid."""
        if not self.contains(price) or self.step <= 0:
            return False
        return (price - self.start) % self.step == 0


class NormalizedMarket(BaseModel):
    """A single binary market, with prices as ``Decimal``.

    Field aliases map the API's ``*_dollars`` / ``*_fp`` names onto clean attribute names.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    ticker: str
    event_ticker: str
    status: MarketStatus
    title: str = ""
    yes_sub_title: str = ""

    notional: Decimal = Field(default=Decimal(1), alias="notional_value_dollars")
    yes_bid: Decimal = Field(default=Decimal(0), alias="yes_bid_dollars")
    yes_ask: Decimal = Field(default=Decimal(1), alias="yes_ask_dollars")
    no_bid: Decimal = Field(default=Decimal(0), alias="no_bid_dollars")
    no_ask: Decimal = Field(default=Decimal(1), alias="no_ask_dollars")
    yes_bid_size: Decimal = Field(default=Decimal(0), alias="yes_bid_size_fp")
    yes_ask_size: Decimal = Field(default=Decimal(0), alias="yes_ask_size_fp")
    last_price: Decimal = Field(default=Decimal(0), alias="last_price_dollars")

    volume: Decimal = Field(default=Decimal(0), alias="volume_fp")
    volume_24h: Decimal = Field(default=Decimal(0), alias="volume_24h_fp")
    open_interest: Decimal = Field(default=Decimal(0), alias="open_interest_fp")
    liquidity: Decimal = Field(default=Decimal(0), alias="liquidity_dollars")

    open_time: datetime | None = None
    close_time: datetime | None = None
    expected_expiration_time: datetime | None = None
    latest_expiration_time: datetime | None = None
    settlement_timer_seconds: int = 0
    can_close_early: bool = False
    result: str = ""

    strike_type: StrikeType | None = None
    floor_strike: Decimal | None = None
    cap_strike: Decimal | None = None

    price_ranges: list[PriceBand] = Field(default_factory=list)
    price_level_structure: str = ""
    exchange_index: int = 0
    rules_primary: str = ""

    @field_validator(
        "notional",
        "yes_bid",
        "yes_ask",
        "no_bid",
        "no_ask",
        "yes_bid_size",
        "yes_ask_size",
        "last_price",
        "volume",
        "volume_24h",
        "open_interest",
        "liquidity",
        mode="before",
    )
    @classmethod
    def _decimal_from_fixed_point(cls, value: object) -> object:
        """Parse fixed-point money strings exactly, refusing floats.

        Every price and count the API sends is a fixed-point *string*. A float arriving here
        means either a caller built the payload wrongly or the API changed representation --
        both worth failing on, since a float silently loses the sub-cent precision the API
        guarantees.
        """
        if isinstance(value, float):
            raise ValueError("float is not accepted for a money field; expected a string")
        if isinstance(value, str):
            return Decimal(value) if value else Decimal(0)
        return value

    @field_validator("floor_strike", "cap_strike", mode="before")
    @classmethod
    def _decimal_from_strike(cls, value: object) -> object:
        """Parse a strike value.

        Unlike prices, strikes are genuine JSON doubles in the API (``format: double``) -- they
        are values of the *underlying*, such as a temperature of 34.5 or an index level, not
        dollar amounts. So floats are accepted here, converted via ``str`` so the decimal
        literal is preserved rather than the binary approximation.
        """
        if isinstance(value, float | int):
            return Decimal(str(value))
        if isinstance(value, str):
            return Decimal(value) if value else None
        return value

    @field_validator("strike_type", mode="before")
    @classmethod
    def _blank_strike_is_none(cls, value: object) -> object:
        return None if value == "" else value

    # -- derived quantities ------------------------------------------------

    @property
    def spread(self) -> Decimal:
        """``yes_ask - yes_bid``, the cost of crossing this market."""
        return self.yes_ask - self.yes_bid

    @property
    def mid(self) -> Decimal:
        return (self.yes_bid + self.yes_ask) / 2

    @property
    def has_yes_ask(self) -> bool:
        """Whether the YES ask reflects a real resting order.

        ``yes_ask == notional`` is what you see both when nobody bids NO *and* when someone
        bids NO at zero. The top-of-book fields cannot tell those apart, so anything that
        needs executable size must consult the book. Treating it as a price is a designed-in
        false-arbitrage trap.
        """
        return self.no_bid > 0

    @property
    def has_no_ask(self) -> bool:
        return self.yes_bid > 0

    @property
    def is_two_sided(self) -> bool:
        """Both sides quotable. A one-sided market cannot support a round trip."""
        return self.yes_bid > 0 and self.no_bid > 0

    @property
    def book_is_uncrossed(self) -> bool:
        """``yes_bid + no_bid <= notional``. False means bad data, not free money."""
        return self.yes_bid + self.no_bid <= self.notional

    def tick_at(self, price: Decimal) -> Decimal | None:
        """Tick size applying at ``price``, or ``None`` if off-grid.

        Tick size is per-market and genuinely sub-cent on some markets, so it is read from
        ``price_ranges`` rather than assumed.
        """
        for band in self.price_ranges:
            if band.contains(price):
                return band.step
        return None

    def is_valid_price(self, price: Decimal) -> bool:
        """Whether an order at ``price`` would be accepted. Off-grid prices are rejected."""
        if not self.price_ranges:
            return Decimal(0) <= price <= self.notional
        return any(band.is_on_grid(price) for band in self.price_ranges)


class NormalizedEvent(BaseModel):
    """A group of markets over one underlying question."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    event_ticker: str
    series_ticker: str = ""
    title: str = ""
    sub_title: str = ""
    mutually_exclusive: bool = False
    collateral_return_type: str = ""
    strike_date: datetime | None = None
    strike_period: str | None = None
    markets: list[NormalizedMarket] = Field(default_factory=list)

    @property
    def exclusivity(self) -> Exclusivity:
        """Exclusivity class, mapped conservatively.

        The API's flag proves only **at most one** YES. Exhaustiveness is not published, so
        this never returns :attr:`Exclusivity.PARTITION` -- that requires a separate proof,
        and assuming it is what turns a losing "buy all YES" basket into a confident one.
        """
        return (
            Exclusivity.MUTUALLY_EXCLUSIVE if self.mutually_exclusive else Exclusivity.UNCONSTRAINED
        )

    @property
    def active_markets(self) -> list[NormalizedMarket]:
        return [m for m in self.markets if m.status.is_tradeable]

    def yes_bid_sum(self) -> Decimal:
        """Sum of YES bids across active legs.

        For a mutually exclusive event, exceeding ``notional`` is the overround condition --
        the direction that is safe without an exhaustiveness proof.
        """
        return sum((m.yes_bid for m in self.active_markets), Decimal(0))

    def yes_ask_sum(self) -> Decimal:
        """Sum of YES asks across active legs. Below ``notional`` is the underround."""
        return sum((m.yes_ask for m in self.active_markets), Decimal(0))


class NormalizedSeries(BaseModel):
    """A series, which carries the fee terms its markets inherit."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    ticker: str
    title: str = ""
    category: str = ""
    fee_type: str = "quadratic"
    fee_multiplier: Decimal = Decimal(1)
    """The schedule's per-series ``M``. **Not** a rate -- see kalshi_arb.fees."""

    @field_validator("fee_multiplier", mode="before")
    @classmethod
    def _multiplier_to_decimal(cls, value: object) -> object:
        # The API sends this as a JSON number; str() first so binary float error never
        # reaches the fee math.
        if isinstance(value, float | int):
            return Decimal(str(value))
        return value
