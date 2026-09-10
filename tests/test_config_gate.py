"""Trading-mode gate tests.

The gate is the boundary between research and real money. These tests assert it **fails
closed** on every ambiguous input, because the failure mode being prevented -- live orders
from a half-configured deployment -- is the most expensive one in the system.
"""

from decimal import Decimal

import pytest

from kalshi_arb.config import (
    ConfigError,
    Environment,
    KalshiEndpoints,
    Settings,
    TradingMode,
    load_settings,
)

D = Decimal


def env(**overrides: str) -> dict[str, str]:
    return dict(overrides)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_default_is_paper_with_no_configuration():
    """An empty environment must never yield live trading."""
    s = load_settings("nonexistent.yaml", env=env())
    assert s.mode is TradingMode.PAPER
    assert not s.is_live
    assert s.environment is Environment.DEMO


def test_missing_config_file_is_not_fatal():
    s = load_settings("nonexistent.yaml", env=env())
    assert s.values == {}
    assert s.section("risk") == {}


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_live_requires_both_switches():
    """TRADING_MODE=live alone is downgraded, and the refusal is recorded."""
    s = load_settings("nonexistent.yaml", env=env(TRADING_MODE="live"))
    assert s.mode is TradingMode.PAPER
    assert s.live_trading_requested is True
    assert s.gate_refusal is not None and "ENABLE_LIVE_TRADING" in s.gate_refusal


def test_live_requires_production_environment():
    """Real orders against a demo endpoint is a configuration error, not a downgrade."""
    with pytest.raises(ConfigError, match="production"):
        load_settings(
            "nonexistent.yaml",
            env=env(TRADING_MODE="live", ENABLE_LIVE_TRADING="true", KALSHI_ENVIRONMENT="demo"),
        )


def test_live_granted_when_fully_configured():
    s = load_settings(
        "nonexistent.yaml",
        env=env(
            TRADING_MODE="live",
            ENABLE_LIVE_TRADING="true",
            KALSHI_ENVIRONMENT="production",
        ),
    )
    assert s.mode is TradingMode.LIVE
    assert s.is_live
    assert s.gate_refusal is None


@pytest.mark.parametrize("value", ["maybe", "TRUE-ish", "2", "yes please", "1.0"])
def test_ambiguous_enable_flag_raises_rather_than_authorising(value):
    """An unparseable flag must never be read as permission."""
    with pytest.raises(ConfigError, match="ENABLE_LIVE_TRADING"):
        load_settings(
            "nonexistent.yaml",
            env=env(TRADING_MODE="live", ENABLE_LIVE_TRADING=value),
        )


@pytest.mark.parametrize("value", ["false", "0", "no", "off", ""])
def test_falsey_enable_flag_downgrades(value):
    s = load_settings("nonexistent.yaml", env=env(TRADING_MODE="live", ENABLE_LIVE_TRADING=value))
    assert s.mode is TradingMode.PAPER


def test_unknown_mode_raises():
    with pytest.raises(ConfigError, match="unknown mode"):
        load_settings("nonexistent.yaml", env=env(TRADING_MODE="yolo"))


def test_enable_flag_alone_does_not_go_live():
    """The flag without the mode stays paper and is not even a refusal."""
    s = load_settings("nonexistent.yaml", env=env(ENABLE_LIVE_TRADING="true"))
    assert s.mode is TradingMode.PAPER
    assert s.live_trading_requested is False


@pytest.mark.parametrize("mode", ["research", "backtest", "paper"])
def test_non_live_modes_place_no_orders(mode):
    s = load_settings("nonexistent.yaml", env=env(TRADING_MODE=mode))
    assert not s.mode.places_real_orders


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


def test_environments_have_distinct_endpoints():
    demo = KalshiEndpoints.for_environment(Environment.DEMO)
    prod = KalshiEndpoints.for_environment(Environment.PRODUCTION)
    assert demo.rest != prod.rest
    assert demo.websocket != prod.websocket
    assert "demo" in demo.rest
    assert demo.rest.startswith("https://")
    assert prod.websocket.startswith("wss://")


def test_signing_prefix_excludes_host():
    """The signed path is host-independent -- signing the host produces a 401."""
    for e in (Environment.DEMO, Environment.PRODUCTION):
        assert KalshiEndpoints.for_environment(e).signing_prefix == "/trade-api/v2"


def test_unknown_environment_raises():
    with pytest.raises(ConfigError, match="unknown environment"):
        load_settings("nonexistent.yaml", env=env(KALSHI_ENVIRONMENT="staging"))


# ---------------------------------------------------------------------------
# Settings values
# ---------------------------------------------------------------------------


def test_decimal_settings_reject_yaml_floats():
    """Money in settings.yaml must be quoted, or precision is lost before it is used."""
    s = Settings(
        mode=TradingMode.PAPER,
        environment=Environment.DEMO,
        endpoints=KalshiEndpoints.for_environment(Environment.DEMO),
        api_tier="basic",
        values={"edge": {"min_net_edge_dollars": 0.01}},
    )
    with pytest.raises(ConfigError, match="quoted strings"):
        s.decimal("edge", "min_net_edge_dollars", "0.01")


def test_decimal_settings_parse_quoted_strings_exactly():
    s = Settings(
        mode=TradingMode.PAPER,
        environment=Environment.DEMO,
        endpoints=KalshiEndpoints.for_environment(Environment.DEMO),
        api_tier="basic",
        values={"edge": {"min_net_edge_dollars": "0.0125"}},
    )
    assert s.decimal("edge", "min_net_edge_dollars", "0.01") == D("0.0125")


def test_shipped_settings_file_loads_and_defaults_to_paper():
    """The committed config must itself be safe."""
    s = load_settings("config/settings.yaml", env=env())
    assert s.mode is TradingMode.PAPER
    assert s.section("risk")
    assert s.decimal("edge", "min_net_edge_dollars", "0") > 0
