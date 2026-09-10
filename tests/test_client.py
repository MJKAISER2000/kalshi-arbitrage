"""REST client tests, using a mock transport so no network is touched.

Focus is on the behaviours that would corrupt a long-running scan: pagination that loops
forever, retries that hammer a rate-limited endpoint, and private calls sent unsigned.
"""

import asyncio
import contextlib
from decimal import Decimal

import httpx
import pytest

from kalshi_arb.api.client import KalshiAPIError, KalshiClient, RateLimitedError
from kalshi_arb.api.ratelimit import Tier
from kalshi_arb.config import ConfigError, Environment, KalshiEndpoints

D = Decimal
ENDPOINTS = KalshiEndpoints.for_environment(Environment.DEMO)


def client_with(handler, **kwargs) -> KalshiClient:
    return KalshiClient(
        endpoints=ENDPOINTS,
        transport=httpx.MockTransport(handler),
        tier=Tier.PRESTIGE,  # avoid throttling delays in tests
        backoff_base=0.0,
        **kwargs,
    )


def run(coro):
    return asyncio.run(coro)


def market_payload(ticker: str = "KXTEST-A") -> dict:
    return {
        "ticker": ticker,
        "event_ticker": "KXTEST",
        "status": "active",
        "yes_bid_dollars": "0.4200",
        "yes_ask_dollars": "0.4400",
        "no_bid_dollars": "0.5600",
        "no_ask_dollars": "0.5800",
    }


# ---------------------------------------------------------------------------
# Basic requests
# ---------------------------------------------------------------------------


def test_get_market_parses_into_the_domain_model():
    async def scenario():
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/markets/KXTEST-A")
            return httpx.Response(200, json={"market": market_payload()})

        async with client_with(handler) as c:
            return await c.get_market("KXTEST-A")

    m = run(scenario())
    assert m.yes_bid == D("0.42")


def test_orderbook_unwraps_the_fixed_point_envelope():
    async def scenario():
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"orderbook_fp": {"yes_dollars": [["0.42", "13.00"]], "no_dollars": []}},
            )

        async with client_with(handler) as c:
            return await c.get_orderbook("KXTEST-A")

    book = run(scenario())
    assert book["yes_dollars"] == [["0.42", "13.00"]]


def test_market_data_works_without_credentials():
    async def scenario():
        def handler(request: httpx.Request) -> httpx.Response:
            assert "KALSHI-ACCESS-KEY" not in request.headers
            return httpx.Response(200, json={"market": market_payload()})

        async with client_with(handler) as c:
            assert not c.is_authenticated
            return await c.get_market("KXTEST-A")

    assert run(scenario()).ticker == "KXTEST-A"


def test_private_endpoint_without_credentials_raises_rather_than_sending():
    """An unsigned private request would fail with an opaque 401; fail clearly instead."""

    async def scenario():
        def handler(_: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
            raise AssertionError("request should never be sent")

        async with client_with(handler) as c:
            await c.get_balance()

    with pytest.raises(ConfigError, match="requires authentication"):
        run(scenario())


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def test_pagination_follows_cursors():
    async def scenario():
        pages = {
            None: {"markets": [market_payload("A")], "cursor": "c1"},
            "c1": {"markets": [market_payload("B")], "cursor": "c2"},
            "c2": {"markets": [market_payload("C")], "cursor": ""},
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=pages[request.url.params.get("cursor")])

        async with client_with(handler) as c:
            return await c.get_markets()

    assert [m.ticker for m in run(scenario())] == ["A", "B", "C"]


def test_repeated_cursor_terminates_pagination():
    """A server-side cursor bug must not spin forever burning the rate-limit budget."""

    async def scenario():
        calls = 0

        def handler(_: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={"markets": [market_payload()], "cursor": "same"})

        async with client_with(handler) as c:
            markets = await c.get_markets()
        return markets, calls

    markets, calls = run(scenario())
    assert calls == 2  # first page, then the repeat is detected
    assert len(markets) == 2


def test_empty_page_terminates_pagination():
    async def scenario():
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"markets": [], "cursor": "next"})

        async with client_with(handler) as c:
            return await c.get_markets()

    assert run(scenario()) == []


def test_max_pages_caps_the_sweep():
    async def scenario():
        calls = 0

        def handler(_: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(
                200, json={"events": [{"event_ticker": f"E{calls}"}], "cursor": f"c{calls}"}
            )

        async with client_with(handler) as c:
            await c.get_events(max_pages=2)
        return calls

    assert run(scenario()) == 2


# ---------------------------------------------------------------------------
# Errors and retries
# ---------------------------------------------------------------------------


def test_retries_transient_server_errors_then_succeeds():
    async def scenario():
        attempts = 0

        def handler(_: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                return httpx.Response(503, text="unavailable")
            return httpx.Response(200, json={"market": market_payload()})

        async with client_with(handler) as c:
            m = await c.get_market("KXTEST-A")
        return m, attempts

    market, attempts = run(scenario())
    assert market.ticker == "KXTEST-A"
    assert attempts == 3


def test_persistent_429_raises_rate_limited_error():
    """429 carries no Retry-After, so retries are bounded and then surfaced."""

    async def scenario():
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"error": "too many requests"})

        async with client_with(handler, max_retries=2) as c:
            await c.get_market("KXTEST-A")

    with pytest.raises(RateLimitedError) as exc:
        run(scenario())
    assert exc.value.status_code == 429


def test_client_errors_are_not_retried():
    """A 404 will never succeed on retry; retrying wastes budget."""

    async def scenario():
        attempts = 0

        def handler(_: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(404, text="not found")

        # contextlib.suppress is a SYNC context manager, so it must nest inside the async
        # one rather than share the `async with` -- combining them raises TypeError.
        async with client_with(handler) as c:
            with contextlib.suppress(KalshiAPIError):
                await c.get_market("NOPE")
        return attempts

    assert run(scenario()) == 1


def test_error_carries_status_and_path_for_the_audit_trail():
    async def scenario():
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(400, text="bad request")

        async with client_with(handler) as c:
            await c.get_market("KXTEST-A")

    with pytest.raises(KalshiAPIError) as exc:
        run(scenario())
    assert exc.value.status_code == 400
    assert "markets/KXTEST-A" in exc.value.path


def test_timeouts_are_retried():
    async def scenario():
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise httpx.ConnectTimeout("timed out", request=request)
            return httpx.Response(200, json={"market": market_payload()})

        async with client_with(handler) as c:
            await c.get_market("KXTEST-A")
        return attempts

    assert run(scenario()) == 2


def test_throttling_is_recorded_for_data_quality():
    """Self-imposed waiting must be observable, not disguised as network latency."""

    async def scenario():
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"market": market_payload()})

        # Basic tier, 200 read tokens: the 21st request in a burst must wait.
        c = KalshiClient(
            endpoints=ENDPOINTS, transport=httpx.MockTransport(handler), tier=Tier.BASIC
        )
        async with c:
            for _ in range(21):
                await c.get_market("KXTEST-A")
            return c.throttled_seconds

    assert run(scenario()) > 0


def test_client_requires_settings_or_endpoints():
    with pytest.raises(ConfigError, match="settings or endpoints"):
        KalshiClient()
