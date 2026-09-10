"""Money primitives.

Kalshi transports prices and quantities as fixed-point *strings* ("0.4200", "13.00"), not
integers and not floats. Sub-cent prices and fractional contracts are both real. Every money
value in this project is a ``Decimal`` parsed straight from those strings; binary floats are
rejected at construction so a rounding error can never enter a P&L path silently.

See docs/KALSHI_API_NOTES.md section 3.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Final

__all__ = [
    "COUNT_DP",
    "DEFAULT_NOTIONAL",
    "MONEY_DP",
    "PRICE_DP",
    "ceil_to",
    "floor_to",
    "parse_count",
    "parse_price",
    "quantize_money",
    "to_decimal",
]

# Decimal places the API guarantees. Sourced from the Fixed-Point Representation doc.
PRICE_DP: Final = Decimal("0.0001")  # prices: up to 4dp
COUNT_DP: Final = Decimal("0.01")  # contract counts: up to 2dp, min granularity 0.01
MONEY_DP: Final = Decimal("0.000001")  # fee math granularity: $0.000001

# Only a fallback for synthetic tests. Production reads ``notional_value_dollars`` per market.
DEFAULT_NOTIONAL: Final = Decimal("1")


class MoneyError(ValueError):
    """Raised when a money value is malformed or outside its permitted domain."""


def to_decimal(value: Decimal | int | str, *, field: str = "value") -> Decimal:
    """Coerce to ``Decimal``, refusing ``float``.

    ``float`` is rejected rather than converted: accepting it would let 0.1 + 0.2 style error
    into fee and payoff math, and the API never requires it.
    """
    if isinstance(value, float):
        raise MoneyError(
            f"{field}: float is not accepted in money paths (got {value!r}); "
            "pass a str or Decimal to preserve exactness"
        )
    if isinstance(value, Decimal):
        dec = value
    elif isinstance(value, (int, str)):
        try:
            dec = Decimal(value)
        except InvalidOperation as exc:
            raise MoneyError(f"{field}: cannot parse {value!r} as a decimal") from exc
    else:
        raise MoneyError(f"{field}: unsupported type {type(value).__name__}")

    if not dec.is_finite():
        raise MoneyError(f"{field}: must be finite (got {value!r})")
    return dec


def parse_price(
    value: Decimal | int | str,
    *,
    notional: Decimal = DEFAULT_NOTIONAL,
    field: str = "price",
) -> Decimal:
    """Parse a price from a Kalshi ``*_dollars`` string.

    A price is bounded by ``[0, notional]``. Both endpoints are legal values that the API does
    emit, so they are accepted here; interpreting them is the caller's job. In particular a
    derived ask equal to 0 or to the notional is *ambiguous* -- it can mean "no liquidity" --
    and must be resolved against book depth, not treated as a tradeable price
    (docs/KALSHI_API_NOTES.md section 4).
    """
    price = to_decimal(value, field=field)
    if price < 0 or price > notional:
        raise MoneyError(f"{field}: {price} outside [0, {notional}]")
    return price


def parse_count(value: Decimal | int | str, *, field: str = "count") -> Decimal:
    """Parse a contract count from a Kalshi ``*_fp`` string. Fractional counts are valid."""
    count = to_decimal(value, field=field)
    if count < 0:
        raise MoneyError(f"{field}: negative count {count}")
    return count


def quantize_money(value: Decimal) -> Decimal:
    """Round a dollar amount to the API's internal $0.000001 fee granularity."""
    return value.quantize(MONEY_DP, rounding=ROUND_HALF_UP)


def ceil_to(value: Decimal, granularity: Decimal) -> Decimal:
    """Round ``value`` up to the next multiple of ``granularity``.

    Used for fees, which the exchange always rounds against the trader.
    """
    if granularity <= 0:
        raise MoneyError(f"granularity must be positive (got {granularity})")
    return (value / granularity).quantize(Decimal(1), rounding=ROUND_CEILING) * granularity


def floor_to(value: Decimal, granularity: Decimal) -> Decimal:
    """Round ``value`` down to a multiple of ``granularity`` (balance alignment)."""
    if granularity <= 0:
        raise MoneyError(f"granularity must be positive (got {granularity})")
    return (value / granularity).quantize(Decimal(1), rounding=ROUND_FLOOR) * granularity
