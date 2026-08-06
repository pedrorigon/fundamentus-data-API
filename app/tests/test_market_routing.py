from __future__ import annotations

import httpx
import pytest

from app.config import Settings
from app.services.market_routing import Market, is_b3_ticker, resolve_market, should_query_b3
from app.services.opportunity import B3InstrumentProvider


@pytest.mark.parametrize(
    "ticker",
    ["PETR4", "BBAS3", "WEGE3", "MXRF11", "WEGE3F", "NVDC34", "AAPL34", "petr4"],
)
def test_brazilian_codes_route_to_b3(ticker: str) -> None:
    assert resolve_market(ticker) is Market.B3
    assert is_b3_ticker(ticker) is True
    assert should_query_b3(ticker) is True


@pytest.mark.parametrize(
    "ticker",
    ["NVDA", "AAPL", "MSFT", "GOOGL", "BRK.B", "TSM", "XUSE", "F", "V", "PETR4.SA"],
)
def test_foreign_codes_do_not_route_to_b3(ticker: str) -> None:
    assert resolve_market(ticker) is Market.FOREIGN
    assert should_query_b3(ticker) is False


def test_an_empty_ticker_is_unknown() -> None:
    assert resolve_market("   ") is Market.UNKNOWN
    assert should_query_b3("") is False


@pytest.mark.asyncio
async def test_b3_provider_skips_the_session_walk_for_a_foreign_ticker() -> None:
    """NVDA cost seven sequential B3 requests that could only return nothing."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    provider = B3InstrumentProvider(
        Settings(),
        transport=httpx.MockTransport(handler),
    )

    assert await provider.get("NVDA") is None
    assert requests == []


@pytest.mark.asyncio
async def test_b3_provider_still_queries_a_brazilian_ticker() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    provider = B3InstrumentProvider(
        Settings(),
        transport=httpx.MockTransport(handler),
    )

    await provider.get("PETR4")

    assert requests


@pytest.mark.asyncio
async def test_the_foreign_negative_answer_is_cached() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    provider = B3InstrumentProvider(
        Settings(),
        transport=httpx.MockTransport(handler),
    )

    assert await provider.get("NVDA") is None
    assert await provider.get("NVDA") is None
    assert requests == []
