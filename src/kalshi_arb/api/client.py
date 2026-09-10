"""Async REST client for the Kalshi Trade API.

Market data is public, so the client works unauthenticated and only requires a signer for
portfolio and order endpoints. That split is enforced: calling a private endpoint without
credentials raises rather than sending an unsigned request that would fail opaquely.

Two behaviours matter for a scanner:

- **Rate limiting is enforced locally**, per bucket, before the request goes out. A 429 from
  Kalshi carries no ``Retry-After`` and no ``X-RateLimit-*`` headers, so there is nothing to
  back off against once you get one.
- **Pagination is cursor-based.** :meth:`KalshiClient.paginate` follows cursors and stops when
  one repeats, so a server-side cursor bug cannot spin forever.

Ordering endpoints are deliberately **not** implemented here. This is Phase 4 -- ingestion
only. Adding them before the paper-trading gate exists would put an order path in reach of a
misconfiguration.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator
from types import TracebackType
from typing import Any

import httpx

from ..config import ConfigError, KalshiEndpoints, Settings
from ..markets.models import NormalizedEvent, NormalizedMarket, NormalizedSeries
from .auth import RequestSigner
from .ratelimit import DEFAULT_TOKEN_COST, Bucket, RateLimiter, Tier

__all__ = ["KalshiAPIError", "KalshiClient", "RateLimitedError"]

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_PAGE_LIMIT = 200
"""Largest page the markets/events endpoints accept."""


class KalshiAPIError(RuntimeError):
    """A non-retryable API error, carrying the status and body for the audit trail."""

    def __init__(self, status_code: int, message: str, *, path: str) -> None:
        super().__init__(f"{status_code} from {path}: {message}")
        self.status_code = status_code
        self.path = path
        self.message = message


class RateLimitedError(KalshiAPIError):
    """Retries were exhausted against repeated 429s."""


class KalshiClient:
    """Async client for market data and portfolio reads.

    Use as an async context manager so the underlying connection pool is closed::

        async with KalshiClient(settings) as client:
            markets = await client.get_markets(status="open")
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        endpoints: KalshiEndpoints | None = None,
        signer: RequestSigner | None = None,
        tier: Tier | str = Tier.BASIC,
        timeout: float = 10.0,
        max_retries: int = 3,
        backoff_base: float = 0.1,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if settings is not None:
            endpoints = endpoints or settings.endpoints
            tier = settings.api_tier
        if endpoints is None:
            raise ConfigError("KalshiClient needs either settings or endpoints")

        self._endpoints = endpoints
        self._signer = signer
        self._limiter = RateLimiter(tier)
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._client = httpx.AsyncClient(
            base_url=endpoints.rest,
            timeout=timeout,
            transport=transport,
            headers={"Accept": "application/json"},
        )
        self.throttled_seconds = 0.0
        """Cumulative time spent waiting on our own rate limiter, for data-quality reporting."""

    async def __aenter__(self) -> KalshiClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def is_authenticated(self) -> bool:
        return self._signer is not None

    # -- request plumbing --------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        authenticated: bool = False,
        bucket: Bucket | None = None,
        cost: int = DEFAULT_TOKEN_COST,
    ) -> dict[str, Any]:
        """Issue one request, respecting rate limits and retrying transient failures.

        Retries use exponential backoff with jitter. Jitter matters because several coroutines
        throttled by the same 429 would otherwise retry in lockstep and trigger it again.
        """
        if authenticated and self._signer is None:
            raise ConfigError(
                f"{path} requires authentication but no credentials were configured. "
                "Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH."
            )

        if bucket is None:
            bucket = Bucket.READ if method.upper() == "GET" else Bucket.WRITE

        last_error: str = ""
        for attempt in range(self._max_retries + 1):
            self.throttled_seconds += await self._limiter.acquire(bucket, cost)

            headers = self._signer.headers(method, path) if self._signer and authenticated else {}
            try:
                response = await self._client.request(method, path, params=params, headers=headers)
            except httpx.TimeoutException as exc:
                last_error = f"timeout: {exc}"
            except httpx.TransportError as exc:
                last_error = f"transport error: {exc}"
            else:
                if response.status_code < 400:
                    payload: dict[str, Any] = response.json()
                    return payload
                last_error = response.text[:500]
                if response.status_code not in RETRYABLE_STATUS:
                    raise KalshiAPIError(response.status_code, last_error, path=path)
                if attempt == self._max_retries and response.status_code == 429:
                    raise RateLimitedError(429, last_error, path=path)

            if attempt < self._max_retries:
                delay = self._backoff_base * (2**attempt)
                await asyncio.sleep(delay * (0.5 + random.random()))

        raise KalshiAPIError(503, f"exhausted retries: {last_error}", path=path)

    async def get(self, path: str, **kwargs: Any) -> dict[str, Any]:
        return await self.request("GET", path, **kwargs)

    async def paginate(
        self,
        path: str,
        key: str,
        *,
        params: dict[str, Any] | None = None,
        authenticated: bool = False,
        limit: int = MAX_PAGE_LIMIT,
        max_pages: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield every item across cursor-paginated pages.

        Stops on an empty page, a missing cursor, or a repeated cursor. That last guard turns
        a server-side pagination bug into a clean stop instead of an unbounded loop that
        silently burns the whole rate-limit budget.
        """
        query = dict(params or {})
        query["limit"] = limit
        seen: set[str] = set()
        pages = 0

        while True:
            payload = await self.get(path, params=query, authenticated=authenticated)
            items = payload.get(key) or []
            for item in items:
                yield item

            pages += 1
            if max_pages is not None and pages >= max_pages:
                return
            cursor = payload.get("cursor")
            if not items or not cursor or cursor in seen:
                return
            seen.add(cursor)
            query["cursor"] = cursor

    # -- public market data ------------------------------------------------

    async def get_exchange_status(self) -> dict[str, Any]:
        """Exchange and per-shard status.

        Shards pause independently, so a scanner must check the shard a market lives on rather
        than only the global ``trading_active`` flag.
        """
        return await self.get("/exchange/status")

    async def get_market(self, ticker: str) -> NormalizedMarket:
        payload = await self.get(f"/markets/{ticker}")
        return NormalizedMarket.model_validate(payload["market"])

    async def get_markets(
        self, *, status: str | None = None, event_ticker: str | None = None, limit: int = 200
    ) -> list[NormalizedMarket]:
        params: dict[str, Any] = {}
        if status:
            params["status"] = status
        if event_ticker:
            params["event_ticker"] = event_ticker
        return [
            NormalizedMarket.model_validate(m)
            async for m in self.paginate("/markets", "markets", params=params, limit=limit)
        ]

    async def get_event(self, event_ticker: str, *, nested: bool = True) -> NormalizedEvent:
        payload = await self.get(f"/events/{event_ticker}", params={"with_nested_markets": nested})
        return NormalizedEvent.model_validate(payload["event"])

    async def get_events(
        self,
        *,
        status: str | None = None,
        nested: bool = True,
        limit: int = 200,
        max_pages: int | None = None,
    ) -> list[NormalizedEvent]:
        params: dict[str, Any] = {"with_nested_markets": nested}
        if status:
            params["status"] = status
        return [
            NormalizedEvent.model_validate(e)
            async for e in self.paginate(
                "/events", "events", params=params, limit=limit, max_pages=max_pages
            )
        ]

    async def get_series(self, series_ticker: str) -> NormalizedSeries:
        """Series metadata, including the fee terms its markets inherit."""
        payload = await self.get(f"/series/{series_ticker}")
        return NormalizedSeries.model_validate(payload["series"])

    async def get_orderbook(self, ticker: str, *, depth: int | None = None) -> dict[str, Any]:
        """Raw order book for one market.

        Returns the payload unparsed; :mod:`kalshi_arb.orderbook` owns interpretation. Note
        the response contains **bids only** on both sides.
        """
        params = {"depth": depth} if depth is not None else None
        payload = await self.get(f"/markets/{ticker}/orderbook", params=params)
        return payload.get("orderbook_fp") or payload.get("orderbook") or {}

    async def get_trades(
        self, *, ticker: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if ticker:
            params["ticker"] = ticker
        return [
            t
            async for t in self.paginate(
                "/markets/trades", "trades", params=params, limit=limit, max_pages=1
            )
        ]

    # -- authenticated reads -----------------------------------------------

    async def get_balance(self) -> dict[str, Any]:
        return await self.get("/portfolio/balance", authenticated=True)

    async def get_positions(self) -> list[dict[str, Any]]:
        return [
            p
            async for p in self.paginate(
                "/portfolio/positions", "market_positions", authenticated=True
            )
        ]

    async def get_fills(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """Fills, which carry ``taker_fees_dollars`` / ``maker_fees_dollars``.

        These are what the fee model gets reconciled against -- the step that turns modelled
        fees from an estimate into ground truth.
        """
        return [
            f
            async for f in self.paginate(
                "/portfolio/fills", "fills", authenticated=True, limit=limit
            )
        ]
