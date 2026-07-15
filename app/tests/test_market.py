from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr

from app.api.dependencies import get_instrument_data_service
from app.config import Settings
from app.core.errors import InvalidTickerError
from app.main import create_app
from app.models import (
    FundProfile,
    InstrumentMetadata,
    InstrumentType,
    InternationalFundamentals,
    MarketQuote,
)
from app.services.market import (
    AlphaVantageInstrumentDataProvider,
    BrapiInstrumentDataProvider,
    InstrumentDataService,
    _allocations,
    _date,
    _datetime,
    _decimal,
    _holdings,
)


def _settings() -> Settings:
    return Settings(
        brapi_token=SecretStr("brapi-secret"),
        alpha_vantage_api_key=SecretStr("alpha-secret"),
    )


def test_market_normalizers_reject_invalid_values_and_collections() -> None:
    assert _decimal("not-a-number") is None
    assert _date(None) is None
    assert _date("invalid") is None
    assert _datetime("invalid") is None
    assert _datetime(None) is None
    assert _allocations({}) == []
    assert _holdings({}) == []


@pytest.mark.asyncio
async def test_brapi_provider_maps_quote_and_market_data() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer brapi-secret"
        assert request.url.params["symbols"] == "BOVA11"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "data": {
                            "longName": "iShares Ibovespa ETF",
                            "currency": "BRL",
                            "regularMarketPrice": 172.76,
                            "regularMarketTime": "2026-07-15T15:30:00Z",
                            "marketCap": 123456,
                        }
                    }
                ]
            },
        )

    provider = BrapiInstrumentDataProvider(_settings(), httpx.MockTransport(handler))

    quote, fundamentals = await provider.get("BOVA11")

    assert quote is not None
    assert quote.price == Decimal("172.76")
    assert quote.currency == "BRL"
    assert quote.quoted_at == datetime(2026, 7, 15, 15, 30, tzinfo=UTC)
    assert fundamentals is not None
    assert fundamentals.market_capitalization == Decimal("123456")
    assert fundamentals.description == "iShares Ibovespa ETF"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404, 429])
async def test_brapi_provider_treats_expected_upstream_status_as_unavailable(status: int) -> None:
    provider = BrapiInstrumentDataProvider(
        Settings(),
        httpx.MockTransport(lambda _request: httpx.Response(status)),
    )

    assert await provider.get("BOVA11") == (None, None)


@pytest.mark.asyncio
async def test_brapi_provider_ignores_empty_or_priceless_results() -> None:
    responses = iter(
        [
            httpx.Response(200, json={"results": []}),
            httpx.Response(200, json={"results": [{"data": {"currency": "BRL"}}]}),
        ]
    )
    provider = BrapiInstrumentDataProvider(
        Settings(),
        httpx.MockTransport(lambda _request: next(responses)),
    )

    assert await provider.get("EMPTY11") == (None, None)
    quote, fundamentals = await provider.get("NOPRICE11")
    assert quote is None
    assert fundamentals is not None


@pytest.mark.asyncio
async def test_brapi_provider_omits_blank_authorization_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        return httpx.Response(404)

    provider = BrapiInstrumentDataProvider(
        Settings(brapi_token=SecretStr("")),
        httpx.MockTransport(handler),
    )

    assert await provider.get("BOVA11") == (None, None)


@pytest.mark.asyncio
async def test_alpha_provider_maps_etf_profile() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["function"] == "ETF_PROFILE"
        assert request.url.params["apikey"] == "alpha-secret"
        return httpx.Response(
            200,
            json={
                "net_assets": "1000000",
                "net_expense_ratio": "0.03",
                "portfolio_turnover": "0.08",
                "dividend_yield": "0.012",
                "net_asset_value": "550.25",
                "inception_date": "2010-09-07",
                "description": "Tracks the S&P 500",
                "sectors": [{"sector": "Technology", "weight": "0.32"}],
                "asset_allocation": [{"name": "Equity", "weight": "0.99"}],
                "holdings": [
                    {"symbol": "AAPL", "description": "Apple Inc.", "weight": "0.07"},
                    {"symbol": "CASH", "weight": "-"},
                ],
            },
        )

    provider = AlphaVantageInstrumentDataProvider(_settings(), httpx.MockTransport(handler))

    profile, fundamentals = await provider.get("VOO", InstrumentType.etf)

    assert fundamentals is None
    assert profile is not None
    assert profile.net_assets == Decimal("1000000")
    assert profile.inception_date == date(2010, 9, 7)
    assert profile.sectors[0].name == "Technology"
    assert profile.holdings[0].symbol == "AAPL"
    assert len(profile.holdings) == 1


@pytest.mark.asyncio
async def test_alpha_provider_maps_stock_fundamentals() -> None:
    payload = {
        "Description": "Apple description",
        "Country": "USA",
        "Sector": "Technology",
        "Industry": "Consumer Electronics",
        "Exchange": "NASDAQ",
        "Currency": "USD",
        "MarketCapitalization": "3000000000",
        "PERatio": "31.5",
        "PriceToBookRatio": "12.2",
        "EPS": "7.1",
        "DividendYield": "0.004",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["function"] == "OVERVIEW"
        return httpx.Response(200, json=payload)

    provider = AlphaVantageInstrumentDataProvider(_settings(), httpx.MockTransport(handler))

    profile, fundamentals = await provider.get("AAPL", InstrumentType.stock)

    assert profile is None
    assert fundamentals is not None
    assert fundamentals.country == "USA"
    assert fundamentals.price_to_earnings == Decimal("31.5")
    assert fundamentals.source == "alpha_vantage"


@pytest.mark.asyncio
async def test_alpha_provider_requires_key_and_handles_rate_limit_payload() -> None:
    without_key = AlphaVantageInstrumentDataProvider(
        Settings(),
        httpx.MockTransport(lambda _request: pytest.fail("network should not be called")),
    )
    assert await without_key.get("AAPL", InstrumentType.stock) == (None, None)

    blank_key = AlphaVantageInstrumentDataProvider(
        Settings(alpha_vantage_api_key=SecretStr("")),
        httpx.MockTransport(lambda _request: pytest.fail("network should not be called")),
    )
    assert await blank_key.get("VOO", InstrumentType.etf) == (None, None)

    provider = AlphaVantageInstrumentDataProvider(
        _settings(),
        httpx.MockTransport(lambda _request: httpx.Response(200, json={"Note": "limited"})),
    )
    assert await provider.get("AAPL", InstrumentType.stock) == (None, None)


class _B3:
    def __init__(self, instrument: InstrumentMetadata | None) -> None:
        self.instrument = instrument
        self.calls = 0

    async def get(self, _ticker: str) -> InstrumentMetadata | None:
        self.calls += 1
        return self.instrument


class _Brapi:
    async def get(
        self, _ticker: str
    ) -> tuple[MarketQuote | None, InternationalFundamentals | None]:
        return None, None


class _Alpha:
    def __init__(self) -> None:
        self.requested_type: InstrumentType | None = None

    async def get(
        self, _ticker: str, instrument_type: InstrumentType
    ) -> tuple[FundProfile | None, InternationalFundamentals | None]:
        self.requested_type = instrument_type
        return None, None


@pytest.mark.asyncio
async def test_instrument_data_service_uses_brapi_for_b3_and_caches_result() -> None:
    instrument = InstrumentMetadata(ticker="BOVA11", instrument_type=InstrumentType.etf)
    b3 = _B3(instrument)
    service = InstrumentDataService(
        Settings(instrument_data_ttl_seconds=60),
        b3=b3,  # type: ignore[arg-type]
        brapi=_Brapi(),  # type: ignore[arg-type]
        alpha=_Alpha(),  # type: ignore[arg-type]
    )

    first = await service.get("bova11")
    second = await service.get("BOVA11")

    assert first is second
    assert first.instrument is instrument
    assert b3.calls == 1


@pytest.mark.asyncio
async def test_instrument_data_service_builds_international_instrument() -> None:
    alpha = _Alpha()
    service = InstrumentDataService(
        Settings(),
        b3=_B3(None),  # type: ignore[arg-type]
        brapi=_Brapi(),  # type: ignore[arg-type]
        alpha=alpha,  # type: ignore[arg-type]
    )

    result = await service.get("voo", InstrumentType.etf)

    assert result.instrument is not None
    assert result.instrument.instrument_type is InstrumentType.etf
    assert result.instrument.category == "INTERNATIONAL"
    assert result.instrument.source == "alpha_vantage"
    assert alpha.requested_type is InstrumentType.etf


@pytest.mark.asyncio
async def test_instrument_data_service_rejects_unsafe_ticker() -> None:
    service = InstrumentDataService(Settings())

    with pytest.raises(InvalidTickerError):
        await service.get("INVALID/TICKER", InstrumentType.etf)


@pytest.mark.asyncio
async def test_instrument_data_endpoint_returns_etf_profile() -> None:
    service = InstrumentDataService(
        Settings(),
        b3=_B3(None),  # type: ignore[arg-type]
        brapi=_Brapi(),  # type: ignore[arg-type]
        alpha=_Alpha(),  # type: ignore[arg-type]
    )
    app = create_app()
    app.dependency_overrides[get_instrument_data_service] = lambda: service

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            "/v2/instruments/VOO",
            params={"instrument_type": "etf"},
        )

    assert response.status_code == 200
    assert response.json()["instrument"]["instrument_type"] == "etf"
