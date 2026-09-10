"""Kalshi fee model.

Fees are the deciding term, not a refinement. A live 8-leg basket measured on 2026-09-09 had
a genuine $0.50 gross edge and $0.62-$0.67 of fees -- see docs/STRATEGIES.md section 2.
Nothing in this project may compute an edge without routing through this module.

**Formulas** (official Kalshi Fee Schedule, effective 2026-07-07)::

    taker fee = round_up( M_taker * 0.07   * C * P * (1 - P) )     M_taker default 1
    maker fee = round_up( M_maker * 0.0175 * C * P * (1 - P) )     M_maker default 0

``M`` is a **per-series multiplier**, not the rate itself. This distinction matters: the API's
``fee_multiplier`` field carries ``M``, so wiring it in as the rate would overstate fees ~14x.
:meth:`FeeSchedule.from_api` is the safe seam for API values.

Note ``0.0175 = 0.25 * 0.07``, so a maker at ``M_maker = 1`` pays a quarter of the taker rate
and at ``M_maker = 2`` (combos, e.g. ``KXMVE``) pays half -- matching the ``fee_type`` names.

**Two properties drive strategy design:**

1. The fee is quadratic in price, peaking at ``price = notional/2``. Mid-priced contracts are
   the most expensive to trade.
2. Rounding is **per order**. An N-leg basket pays N independent round-ups, so basket fees
   scale with leg count while the arbitrage edge does not.

Rates are **not** hard-coded per market: ``fee_type`` and ``fee_multiplier`` are read from the
API (Series, with per-Event override). Unmodelled cases raise rather than guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum, StrEnum

from ..types import DEFAULT_NOTIONAL, MoneyError, ceil_to, parse_count, parse_price, to_decimal

__all__ = [
    "MAKER_BASE_RATE",
    "TAKER_BASE_RATE",
    "FeeBreakdown",
    "FeeModel",
    "FeeSchedule",
    "FeeType",
    "MemberPrecision",
    "Role",
    "UnmodelledFeeError",
]

# Base rates from the official fee schedule. The per-series multiplier M scales these.
TAKER_BASE_RATE = Decimal("0.07")
MAKER_BASE_RATE = Decimal("0.0175")  # == 0.25 * TAKER_BASE_RATE

# Per-series maker multiplier M implied by each fee_type, per the schedule's
# "Non-Standard Fees" table (e.g. KXMVE combos carry maker M = 2).
_MAKER_M_BY_FEE_TYPE: dict[str, Decimal] = {
    "quadratic": Decimal(0),
    "quadratic_with_maker_fees": Decimal(1),
    "quadratic_with_combo_maker_fees": Decimal(2),
}


class UnmodelledFeeError(NotImplementedError):
    """Raised when a fee structure is not modelled.

    Deliberately fatal. Guessing a fee is worse than refusing to price the trade: it produces
    confident, wrong edges. The caller must supply a verified schedule instead.
    """


class FeeType(StrEnum):
    """Fee structures the API reports on a Series (``fee_type``)."""

    QUADRATIC = "quadratic"
    """General Trading Fees Table. Taker pays; resting maker orders pay nothing (M_maker = 0)."""

    QUADRATIC_WITH_MAKER_FEES = "quadratic_with_maker_fees"
    """As above with maker M = 1, i.e. a quarter of the taker rate."""

    QUADRATIC_WITH_COMBO_MAKER_FEES = "quadratic_with_combo_maker_fees"
    """Combo markets: maker M = 2, i.e. half the taker rate."""

    FLAT = "flat"
    """Legacy per-contract flat schedule.

    The current schedule expresses non-standard pricing through per-series multipliers rather
    than a flat table, so this is left unmodelled: :class:`FeeModel` raises
    :class:`UnmodelledFeeError` unless a verified ``flat_fee_per_contract`` is configured.
    """


class Role(StrEnum):
    """Which side of the match the order was on. Determines which base rate applies."""

    TAKER = "taker"
    MAKER = "maker"


class MemberPrecision(Decimal, Enum):
    """Balance precision fees are aligned to, which sets the round-up granularity.

    The fee schedule states the round-up brings fee plus position cost to a **centicent**
    ($0.0001), which matches a *direct* member. Members clearing through an FCM align to
    $0.01 instead.

    The default is the coarser :attr:`NON_DIRECT`, deliberately: overstating fees only costs
    us marginal trades, whereas understating them manufactures arbitrage that is not there.
    A direct member should set :attr:`DIRECT` to avoid leaving real trades on the table.
    """

    NON_DIRECT = Decimal("0.01")
    DIRECT = Decimal("0.0001")


@dataclass(frozen=True)
class FeeSchedule:
    """Fee terms applying to one market.

    ``taker_multiplier`` and ``maker_multiplier`` are the schedule's per-series ``M`` values,
    **not** rates. Leave ``maker_multiplier`` as ``None`` to derive it from ``fee_type``.
    """

    fee_type: FeeType = FeeType.QUADRATIC
    taker_multiplier: Decimal = Decimal(1)
    maker_multiplier: Decimal | None = None
    precision: MemberPrecision = MemberPrecision.NON_DIRECT
    flat_fee_per_contract: Decimal | None = None
    """Only for ``FeeType.FLAT``, and only from a verified schedule."""

    def __post_init__(self) -> None:
        if self.taker_multiplier < 0:
            raise MoneyError(f"taker multiplier must be non-negative (got {self.taker_multiplier})")
        if self.maker_multiplier is not None and self.maker_multiplier < 0:
            raise MoneyError(f"maker multiplier must be non-negative (got {self.maker_multiplier})")
        if self.flat_fee_per_contract is not None and self.flat_fee_per_contract < 0:
            raise MoneyError("flat fee must be non-negative")

    @classmethod
    def from_api(
        cls,
        fee_type: str,
        fee_multiplier: Decimal | int | str,
        *,
        precision: MemberPrecision = MemberPrecision.NON_DIRECT,
    ) -> FeeSchedule:
        """Build a schedule from a Series' ``fee_type`` and ``fee_multiplier``.

        ``fee_multiplier`` is the taker ``M``; the maker ``M`` is implied by ``fee_type``.
        Use this rather than the constructor when the values came from the API, so the
        multiplier is never mistaken for a rate.
        """
        try:
            ftype = FeeType(fee_type)
        except ValueError as exc:
            raise UnmodelledFeeError(
                f"unknown fee_type {fee_type!r}; refusing to price a trade on a guess"
            ) from exc
        return cls(
            fee_type=ftype,
            taker_multiplier=to_decimal(fee_multiplier, field="fee_multiplier"),
            maker_multiplier=_MAKER_M_BY_FEE_TYPE.get(ftype.value),
            precision=precision,
        )

    def effective_maker_multiplier(self) -> Decimal:
        """Maker ``M``, explicit if given, otherwise implied by ``fee_type``."""
        if self.maker_multiplier is not None:
            return self.maker_multiplier
        return _MAKER_M_BY_FEE_TYPE.get(self.fee_type.value, Decimal(0))

    def rate(self, role: Role) -> Decimal:
        """The rate actually applied: base rate times the per-series multiplier."""
        if role is Role.TAKER:
            return TAKER_BASE_RATE * self.taker_multiplier
        return MAKER_BASE_RATE * self.effective_maker_multiplier()


@dataclass(frozen=True)
class FeeBreakdown:
    """A fee, with the inputs that produced it, so any charge can be audited later."""

    fee: Decimal
    raw: Decimal
    """Model fee before rounding -- the gap to ``fee`` is the round-up cost."""
    count: Decimal
    price: Decimal
    role: Role
    schedule: FeeSchedule

    @property
    def rounding_cost(self) -> Decimal:
        return self.fee - self.raw

    @property
    def per_contract(self) -> Decimal:
        return self.fee / self.count if self.count else Decimal(0)


class FeeModel:
    """Computes trading fees. Stateless and pure; safe to share."""

    def raw_fee(
        self,
        count: Decimal | int | str,
        price: Decimal | int | str,
        *,
        schedule: FeeSchedule,
        role: Role = Role.TAKER,
        notional: Decimal = DEFAULT_NOTIONAL,
    ) -> Decimal:
        """Model fee **before** rounding.

        ``rate(role) * count * price * (notional - price) / notional``. Dividing by notional
        keeps the quadratic term dimensionally correct for contracts whose notional is not $1.
        """
        if notional <= 0:
            raise MoneyError(f"notional must be positive (got {notional})")
        qty = parse_count(count)
        px = parse_price(price, notional=notional)

        if schedule.fee_type is FeeType.FLAT:
            if schedule.flat_fee_per_contract is None:
                raise UnmodelledFeeError(
                    "fee_type=flat has no modelled rate. The current schedule prices "
                    "non-standard series through per-series multipliers instead, so supply a "
                    "verified FeeSchedule(flat_fee_per_contract=...) rather than guessing."
                )
            return schedule.flat_fee_per_contract * qty

        return schedule.rate(role) * qty * px * (notional - px) / notional

    def calculate_fee(
        self,
        count: Decimal | int | str,
        price: Decimal | int | str,
        *,
        schedule: FeeSchedule | None = None,
        role: Role = Role.TAKER,
        notional: Decimal = DEFAULT_NOTIONAL,
    ) -> FeeBreakdown:
        """Fee for a single order, rounded up to the member's balance precision.

        Rounding is per **order**, matching the exchange's per-order fee accumulator. A
        zero-rate case (a maker under plain ``quadratic``) rounds to zero rather than to one
        cent, so free maker fills are not misreported as costing money.
        """
        sched = schedule or FeeSchedule()
        raw = self.raw_fee(count, price, schedule=sched, role=role, notional=notional)
        fee = Decimal(0) if raw == 0 else ceil_to(raw, sched.precision.value)
        return FeeBreakdown(
            fee=fee,
            raw=raw,
            count=parse_count(count),
            price=parse_price(price, notional=notional),
            role=role,
            schedule=sched,
        )

    def calculate_basket_cost(
        self,
        legs: list[tuple[Decimal | int | str, Decimal | int | str]],
        *,
        schedule: FeeSchedule | None = None,
        role: Role = Role.TAKER,
        notional: Decimal = DEFAULT_NOTIONAL,
    ) -> Decimal:
        """Total fee for a multi-leg basket, as ``(count, price)`` pairs.

        Each leg is a separate order and therefore rounds **independently** -- the reason
        basket fees grow with leg count. Summing raw fees and rounding once would understate
        the cost and manufacture arbitrage that does not exist.
        """
        return sum(
            (
                self.calculate_fee(
                    count, price, schedule=schedule, role=role, notional=notional
                ).fee
                for count, price in legs
            ),
            Decimal(0),
        )

    def calculate_round_trip_cost(
        self,
        count: Decimal | int | str,
        entry_price: Decimal | int | str,
        exit_price: Decimal | int | str,
        *,
        schedule: FeeSchedule | None = None,
        entry_role: Role = Role.TAKER,
        exit_role: Role = Role.TAKER,
        notional: Decimal = DEFAULT_NOTIONAL,
    ) -> Decimal:
        """Fees to open and then close a position before settlement.

        Holding to settlement incurs only the entry fee -- there is no settlement fee -- so
        this applies to positions exited early.
        """
        entry = self.calculate_fee(
            count, entry_price, schedule=schedule, role=entry_role, notional=notional
        )
        exit_ = self.calculate_fee(
            count, exit_price, schedule=schedule, role=exit_role, notional=notional
        )
        return entry.fee + exit_.fee

    def effective_price(
        self,
        count: Decimal | int | str,
        price: Decimal | int | str,
        *,
        schedule: FeeSchedule | None = None,
        role: Role = Role.TAKER,
        notional: Decimal = DEFAULT_NOTIONAL,
    ) -> Decimal:
        """All-in cost per contract: price plus the fee amortised over the order.

        This is the number strategies must compare against payoffs. Comparing quoted prices
        against payoffs is precisely the error that makes fee-negative baskets look profitable.
        """
        qty = parse_count(count)
        if qty <= 0:
            raise MoneyError(f"count must be positive to amortise a fee (got {qty})")
        breakdown = self.calculate_fee(
            count, price, schedule=schedule, role=role, notional=notional
        )
        return to_decimal(price, field="price") + breakdown.fee / qty
