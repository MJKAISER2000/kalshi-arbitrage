"""Configuration and the trading-mode gate.

The gate exists so that live trading cannot happen by accident. It **fails closed**: anything
missing, malformed, or ambiguous resolves to a non-live mode. Enabling live trading requires
two independent settings to agree, and the resulting object is passed explicitly to whatever
needs it rather than read from a global -- so an execution path cannot reach a live client
without having been handed one.

Secrets come from environment variables only; strategy parameters come from
``config/settings.yaml``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "ConfigError",
    "Credentials",
    "Environment",
    "KalshiEndpoints",
    "Settings",
    "TradingMode",
    "load_settings",
]

TRUE_VALUES = frozenset({"true", "1", "yes", "on"})
FALSE_VALUES = frozenset({"false", "0", "no", "off", ""})


class ConfigError(ValueError):
    """Raised when configuration is missing, malformed, or unsafe."""


class TradingMode(StrEnum):
    """How the platform is allowed to act on what it finds."""

    RESEARCH = "research"
    """Read-only analysis. No orders, simulated or otherwise."""

    BACKTEST = "backtest"
    """Historical replay against recorded data."""

    PAPER = "paper"
    """Live market data, simulated execution. The default."""

    LIVE = "live"
    """Real orders with real money. Requires the full gate to pass."""

    @property
    def places_real_orders(self) -> bool:
        return self is TradingMode.LIVE


class Environment(StrEnum):
    """Kalshi environment. Credentials are not shared between them."""

    DEMO = "demo"
    PRODUCTION = "production"


@dataclass(frozen=True)
class KalshiEndpoints:
    """REST and WebSocket base URLs for one environment."""

    rest: str
    websocket: str

    @classmethod
    def for_environment(cls, env: Environment) -> KalshiEndpoints:
        if env is Environment.PRODUCTION:
            return cls(
                rest="https://external-api.kalshi.com/trade-api/v2",
                websocket="wss://external-api-ws.kalshi.com/trade-api/ws/v2",
            )
        return cls(
            rest="https://external-api.demo.kalshi.co/trade-api/v2",
            websocket="wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2",
        )

    @property
    def signing_prefix(self) -> str:
        """Path prefix that must appear in the signed message, e.g. ``/trade-api/v2``."""
        return "/" + self.rest.split("//", 1)[1].split("/", 1)[1]


@dataclass(frozen=True)
class Credentials:
    """Kalshi API credentials. Loaded from the environment, never from a config file."""

    api_key_id: str
    private_key_path: Path

    @classmethod
    def from_env(cls) -> Credentials:
        key_id = os.environ.get("KALSHI_API_KEY_ID", "").strip()
        key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "").strip()
        if not key_id:
            raise ConfigError("KALSHI_API_KEY_ID is not set")
        if not key_path:
            raise ConfigError("KALSHI_PRIVATE_KEY_PATH is not set")
        path = Path(key_path).expanduser()
        if not path.is_file():
            raise ConfigError(f"private key not found at {path}")
        return cls(api_key_id=key_id, private_key_path=path)


def _parse_bool(raw: str | None, *, field: str) -> bool:
    """Strict boolean parsing. An unrecognised value is an error, never a silent ``True``."""
    value = (raw or "").strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    raise ConfigError(f"{field}: expected a boolean, got {raw!r}")


@dataclass(frozen=True)
class Settings:
    """Resolved configuration.

    ``mode`` is the *effective* mode after the gate has been applied, so callers can trust it
    without re-deriving the safety logic.
    """

    mode: TradingMode
    environment: Environment
    endpoints: KalshiEndpoints
    api_tier: str
    values: dict[str, Any]
    """Parsed ``settings.yaml`` tree, for strategy parameters."""
    live_trading_requested: bool = False
    """Whether live was asked for, regardless of whether it was granted."""
    gate_refusal: str | None = None
    """Why live trading was refused, if it was requested and denied."""

    @property
    def is_live(self) -> bool:
        return self.mode.places_real_orders

    def section(self, name: str) -> dict[str, Any]:
        """A top-level block of settings.yaml, or an empty dict if absent."""
        value = self.values.get(name)
        return value if isinstance(value, dict) else {}

    def decimal(self, section: str, key: str, default: str) -> Decimal:
        """Read a money-ish setting as ``Decimal``, never via ``float``."""
        raw = self.section(section).get(key, default)
        if isinstance(raw, float):
            raise ConfigError(
                f"{section}.{key}: write money values as quoted strings in settings.yaml "
                f"(got the float {raw!r}, which cannot represent decimals exactly)"
            )
        return Decimal(str(raw))


def _resolve_mode(env: dict[str, str]) -> tuple[TradingMode, bool, str | None]:
    """Apply the live-trading gate.

    Returns the effective mode, whether live was requested, and the refusal reason if denied.

    Live requires **both** ``TRADING_MODE=live`` and ``ENABLE_LIVE_TRADING=true``. Requesting
    live without the second is downgraded to paper rather than raising, so a misconfigured
    deployment keeps running safely instead of crashing into an unsupervised restart loop --
    but the refusal is recorded and callers surface it loudly.
    """
    raw_mode = env.get("TRADING_MODE", "").strip().lower() or TradingMode.PAPER.value
    try:
        requested = TradingMode(raw_mode)
    except ValueError as exc:
        raise ConfigError(
            f"TRADING_MODE: unknown mode {raw_mode!r}; "
            f"expected one of {[m.value for m in TradingMode]}"
        ) from exc

    if requested is not TradingMode.LIVE:
        return requested, False, None

    # Live was requested. A malformed ENABLE_LIVE_TRADING raises rather than being read as
    # permission -- an ambiguous flag must never authorise real orders.
    enabled = _parse_bool(env.get("ENABLE_LIVE_TRADING"), field="ENABLE_LIVE_TRADING")
    if not enabled:
        return (
            TradingMode.PAPER,
            True,
            "TRADING_MODE=live but ENABLE_LIVE_TRADING is not true; downgraded to paper",
        )
    return TradingMode.LIVE, True, None


def load_settings(
    config_path: str | Path = "config/settings.yaml",
    *,
    env: dict[str, str] | None = None,
) -> Settings:
    """Load settings, applying the trading-mode gate.

    ``env`` defaults to the process environment; pass a dict in tests so the gate can be
    exercised without mutating global state.
    """
    environ = dict(os.environ) if env is None else env

    # Explicit UTF-8: the platform default on Windows is cp1252 and mangles non-ASCII.
    path = Path(config_path)
    values = yaml.safe_load(path.read_text(encoding="utf-8")) or {} if path.is_file() else {}
    if not isinstance(values, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")

    mode, requested, refusal = _resolve_mode(environ)

    raw_env = environ.get("KALSHI_ENVIRONMENT", "").strip().lower() or Environment.DEMO.value
    try:
        environment = Environment(raw_env)
    except ValueError as exc:
        raise ConfigError(
            f"KALSHI_ENVIRONMENT: unknown environment {raw_env!r}; "
            f"expected one of {[e.value for e in Environment]}"
        ) from exc

    # Guard against the most dangerous single mistake: real orders against production while
    # believing the demo environment is configured.
    if mode.places_real_orders and environment is not Environment.PRODUCTION:
        raise ConfigError(
            "live trading requires KALSHI_ENVIRONMENT=production; "
            f"got {environment.value!r}. Refusing to run live against a demo endpoint."
        )

    return Settings(
        mode=mode,
        environment=environment,
        endpoints=KalshiEndpoints.for_environment(environment),
        api_tier=environ.get("KALSHI_API_TIER", "").strip().lower() or "basic",
        values=values,
        live_trading_requested=requested,
        gate_refusal=refusal,
    )
