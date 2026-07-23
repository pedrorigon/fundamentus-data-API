import asyncio
from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZipFile

import httpx
import pytest
from pydantic import ValidationError

from app.api.dependencies import get_historical_quote_service
from app.cache import CacheStore
from app.config import Settings
from app.main import create_app, lifespan
from app.models.historical_quotes import (
    MAX_HISTORICAL_QUOTE_DATES,
    HistoricalQuoteRequest,
)
from app.scrapers.b3_historical_quotes import (
    B3HistoricalQuoteProvider,
    parse_b3_historical_quotes,
)
from app.services.historical_quotes import HistoricalQuoteService


def _cache() -> CacheStore:
    return CacheStore(sqlite_enabled=False, sqlite_path=Path("unused.sqlite3"))


def _record(
    reference: date,
    ticker: str,
    *,
    price_cents: int,
    factor: int = 1,
    market: str = "010",
) -> str:
    values = [" "] * 245
    values[0:2] = "01"
    values[2:10] = reference.strftime("%Y%m%d")
    values[12:24] = f"{ticker:<12}"
    values[24:27] = market
    values[108:121] = f"{price_cents:013d}"
    values[210:217] = f"{factor:07d}"
    return "".join(values)


def _archive(*records: str) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr("COTAHIST_A2020.TXT", "\n".join(records))
    return buffer.getvalue()


def test_parser_uses_cash_market_last_price_and_quotation_factor() -> None:
    payload = _archive(
        _record(date(2020, 5, 28), "AZUL4", price_cents=1234),
        _record(date(2020, 5, 29), "AZUL4", price_cents=150000, factor=1000),
        _record(date(2020, 5, 29), "AZUL4", price_cents=9999, market="020"),
        _record(date(2020, 5, 29), "OTHER3", price_cents=3000),
    )

    assert parse_b3_historical_quotes(payload, {"AZUL4"}) == {
        "AZUL4": {
            date(2020, 5, 28): Decimal("12.34"),
            date(2020, 5, 29): Decimal("1.5"),
        }
    }


@pytest.mark.asyncio
async def test_provider_downloads_public_annual_archive() -> None:
    payload = _archive(_record(date(2020, 5, 29), "AZUL4", price_cents=1234))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/COTAHIST_A2020.ZIP")
        return httpx.Response(200, content=payload)

    provider = B3HistoricalQuoteProvider(Settings(), httpx.MockTransport(handler))

    assert await provider.prices(2020, {"AZUL4"}) == {
        "AZUL4": {date(2020, 5, 29): Decimal("12.34")}
    }


class _Provider:
    def __init__(self, data: dict[int, dict[str, dict[date, Decimal]]]) -> None:
        self.data = data
        self.calls: list[tuple[int, set[str]]] = []

    async def prices(self, year: int, tickers: set[str]) -> dict[str, dict[date, Decimal]]:
        self.calls.append((year, tickers))
        return {ticker: self.data.get(year, {}).get(ticker, {}) for ticker in tickers}


class _ConcurrentProvider(_Provider):
    def __init__(self) -> None:
        super().__init__({})
        self.active = 0
        self.max_active = 0

    async def prices(self, year: int, tickers: set[str]) -> dict[str, dict[date, Decimal]]:
        self.calls.append((year, tickers))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0)
        self.active -= 1
        return {}


@pytest.mark.asyncio
async def test_service_uses_previous_trading_day_and_caches_annual_series() -> None:
    provider = _Provider({2020: {"AZUL4": {date(2020, 5, 29): Decimal("12.34")}}})
    service = HistoricalQuoteService(Settings(), _cache(), provider=provider)  # type: ignore[arg-type]
    request = HistoricalQuoteRequest(tickers=["azul4", "MISSING3"], dates=[date(2020, 5, 31)])

    first = await service.resolve(request)
    second = await service.resolve(request)

    quote = first.quotes["AZUL4"][0]
    assert quote.requested_date == date(2020, 5, 31)
    assert quote.reference_date == date(2020, 5, 29)
    assert quote.close_price == Decimal("12.34")
    assert first.unavailable == ["MISSING3"]
    assert second == first
    assert provider.calls == [(2019, {"AZUL4", "MISSING3"}), (2020, {"AZUL4", "MISSING3"})]


@pytest.mark.asyncio
async def test_service_loads_missing_annual_archives_concurrently() -> None:
    provider = _ConcurrentProvider()
    settings = Settings(upstream_concurrency=2)
    service = HistoricalQuoteService(settings, _cache(), provider=provider)  # type: ignore[arg-type]

    await service.resolve(
        HistoricalQuoteRequest(
            tickers=["BBAS3"],
            dates=[date(2020, 1, 1), date(2021, 1, 1)],
        )
    )

    assert provider.max_active == 2
    assert {year for year, _tickers in provider.calls} == {2019, 2020, 2021}


def test_request_rejects_unsafe_ticker() -> None:
    with pytest.raises(ValidationError):
        HistoricalQuoteRequest(tickers=["../../secret"], dates=[date(2020, 5, 29)])


def test_request_supports_a_complete_multi_year_daily_history() -> None:
    dates = [
        date(2020, 1, 1) + timedelta(days=index)
        for index in range(MAX_HISTORICAL_QUOTE_DATES)
    ]

    assert len(HistoricalQuoteRequest(tickers=["BBAS3"], dates=dates).dates) == len(dates)
    with pytest.raises(ValidationError):
        HistoricalQuoteRequest(
            tickers=["BBAS3"],
            dates=[*dates, date(2030, 1, 1)],
        )


@pytest.mark.asyncio
async def test_endpoint_returns_resolved_historical_quote() -> None:
    provider = _Provider({2020: {"AZUL4": {date(2020, 5, 29): Decimal("12.34")}}})
    service = HistoricalQuoteService(Settings(), _cache(), provider=provider)  # type: ignore[arg-type]
    app = create_app()
    app.dependency_overrides[get_historical_quote_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1/equities/historical-quotes/resolve",
            json={"tickers": ["AZUL4"], "dates": ["2020-05-31"]},
        )

    assert response.status_code == 200
    assert response.json()["quotes"]["AZUL4"][0] == {
        "ticker": "AZUL4",
        "requested_date": "2020-05-31",
        "reference_date": "2020-05-29",
        "close_price": "12.34",
        "currency": "BRL",
        "source": "b3_cotahist",
    }


def test_historical_quote_dependency_reads_application_state() -> None:
    service = object()
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(historical_quote_service=service))
    )

    assert get_historical_quote_service(request) is service  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_lifespan_registers_historical_quote_service() -> None:
    app = create_app()

    async with lifespan(app):
        assert isinstance(app.state.historical_quote_service, HistoricalQuoteService)
