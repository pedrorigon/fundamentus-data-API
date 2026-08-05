from __future__ import annotations

import asyncio

import httpx
import pytest

from app.config import Settings
from app.models import InstrumentMetadata, InstrumentType, InternationalFundamentals, MarketQuote
from app.scrapers.sec_companyfacts import SecCompanyFactsProvider
from app.services.instrument_directory import (
    BrapiInstrumentDirectoryProvider,
    parse_brapi_directory,
)
from app.services.market import InstrumentDataService, _invoke_directory, _metadata_from_sec


def _settings() -> Settings:
    return Settings(
        sec_company_tickers_url="https://sec.test/company_tickers_exchange.json",
        brapi_base_url="https://brapi.test",
        retry_attempts=1,
        instrument_directory_ttl_seconds=0,
        sec_ticker_map_ttl_seconds=0,
    )


def test_brapi_directory_parses_asset_types_and_accents() -> None:
    results = parse_brapi_directory(
        {
            "stocks": [
                {"stock": "PETR4", "name": "Petróleo Brasileiro", "type": "stock"},
                {"stock": "AAPL34", "name": "Apple BDR", "type": "BDR"},
                {"stock": "HGLG11", "name": "Fundo Imobiliário Logística", "type": "FII"},
                {"stock": "IVVB11", "name": "iShares ETF", "type": "ETF"},
            ]
        }
    )
    assert [item.instrument_type for item in results] == [
        InstrumentType.stock,
        InstrumentType.bdr,
        InstrumentType.fii,
        InstrumentType.etf,
    ]
    assert results[0].country == "BR"
    assert results[0].source == "brapi_directory_complementary"


def test_brapi_directory_accepts_list_shapes_and_rejects_bad_rows() -> None:
    rows = [
        {"ticker": "FIAG11", "name": "FIAGRO Crédito", "type": "FIAGRO"},
        {"ticker": "IFRA11", "name": "FI Infra", "type": "FI-INFRA"},
        {"ticker": "XPCM11", "name": "Unit", "type": "UNIT"},
        {"ticker": "ABCD34", "name": "No type"},
        {"ticker": "ABCD34", "name": "Duplicate"},
        {"ticker": "bad ticker", "name": "Invalid"},
        {"name": "Missing ticker"},
        "not a row",
    ]
    results = parse_brapi_directory(rows)
    assert [item.instrument_type for item in results] == [
        InstrumentType.fiagro,
        InstrumentType.fi_infra,
        InstrumentType.unit,
        InstrumentType.bdr,
    ]
    assert parse_brapi_directory(None) == []
    assert parse_brapi_directory({"nested": rows})


@pytest.mark.asyncio
async def test_brapi_directory_alias_and_unavailable_response() -> None:
    settings = _settings()
    available = BrapiInstrumentDirectoryProvider(
        settings,
        httpx.MockTransport(
            lambda _request: httpx.Response(
                200, json={"stocks": [{"stock": "PETR4", "name": "Petroleo"}]}
            )
        ),
    )
    assert (await available.instruments())[0].ticker == "PETR4"

    unavailable = BrapiInstrumentDirectoryProvider(
        settings,
        httpx.MockTransport(lambda _request: httpx.Response(503)),
    )
    assert await unavailable.directory() == []
    assert unavailable.last_error

    empty = BrapiInstrumentDirectoryProvider(
        settings,
        httpx.MockTransport(lambda _request: httpx.Response(200, json={"stocks": []})),
    )
    assert await empty.directory() == []
    assert empty.last_error == "brapi returned no parseable instruments"


@pytest.mark.asyncio
async def test_warm_merges_sec_and_brazil_sources_and_search_is_local() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host == "sec.test":
            return httpx.Response(
                200,
                json={
                    "fields": ["cik", "name", "ticker", "exchange"],
                    "data": [[1, "Apple Inc", "AAPL", "Nasdaq"], [2, "Petroleo", "PETR4", "B3"]],
                },
            )
        return httpx.Response(
            200,
            json={
                "stocks": [
                    {"stock": "PETR4", "name": "Petróleo Brasileiro", "type": "stock"},
                    {"stock": "AAPL34", "name": "Apple BDR", "type": "BDR"},
                    {"stock": "HGLG11", "name": "Fundo Imobiliário Logística", "type": "FII"},
                ]
            },
        )

    settings = _settings()
    service = InstrumentDataService(
        settings,
        sec=SecCompanyFactsProvider(settings, httpx.MockTransport(handler)),
        brapi_directory=BrapiInstrumentDirectoryProvider(settings, httpx.MockTransport(handler)),
    )
    await service.warm_directory()
    assert len(calls) == 2

    result = await service.search("petróleo", limit=10)
    assert [item.ticker for item in result.results] == ["PETR4"]
    assert result.results[0].name == "Petróleo Brasileiro"
    assert result.results[0].source == "brapi_directory_complementary"

    result = await service.search("apple")
    assert [item.ticker for item in result.results] == ["AAPL", "AAPL34"]
    before = len(calls)
    await service.search("hglg")
    assert len(calls) == before


@pytest.mark.asyncio
async def test_dedupe_keeps_observed_bdr_underlying_metadata() -> None:
    payload = {"stocks": [{"stock": "AAPL34", "name": "Apple BDR", "type": "BDR"}]}
    settings = _settings()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))

    class B3:
        async def get(self, ticker: str) -> InstrumentMetadata:
            return InstrumentMetadata(
                ticker=ticker,
                name="Apple BDR observed",
                instrument_type=InstrumentType.bdr,
                exchange="B3",
                underlying_ticker="AAPL",
                underlying_name="Apple Inc",
                underlying_source="b3",
            )

    class Brapi:
        async def get(
            self, _ticker: str
        ) -> tuple[MarketQuote | None, InternationalFundamentals | None]:
            return None, None

    service = InstrumentDataService(
        settings,
        b3=B3(),  # type: ignore[arg-type]
        brapi=Brapi(),  # type: ignore[arg-type]
        sec=SecCompanyFactsProvider(settings, transport),
        brapi_directory=BrapiInstrumentDirectoryProvider(settings, transport),
    )
    await service.get("AAPL34")
    await service.warm_directory()
    result = await service.search("apple")
    observed = next(item for item in result.results if item.ticker == "AAPL34")
    assert observed.underlying_ticker == "AAPL"
    assert observed.underlying_source == "b3"


@pytest.mark.asyncio
async def test_warm_singleflight_and_stale_fallback_on_provider_outage() -> None:
    calls = 0
    healthy = True

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if not healthy:
            return httpx.Response(503)
        return httpx.Response(200, json={"stocks": [{"stock": "PETR4", "name": "Petroleo"}]})

    settings = _settings()
    service = InstrumentDataService(
        settings,
        sec=SecCompanyFactsProvider(settings, httpx.MockTransport(handler)),
        brapi_directory=BrapiInstrumentDirectoryProvider(settings, httpx.MockTransport(handler)),
    )
    await asyncio.gather(service.warm_directory(), service.warm_directory())
    assert calls == 2
    assert (await service.search("petr")).results

    healthy = False
    await service.warm_directory(force=True)
    stale = await service.search("petr")
    assert stale.results
    assert stale.unavailable_reason and "unavailable" in stale.unavailable_reason


@pytest.mark.asyncio
async def test_search_ranking_ttl_refresh_and_empty_query() -> None:
    settings = Settings(instrument_directory_ttl_seconds=3600)
    service = InstrumentDataService(settings)
    service._remember(
        InstrumentMetadata(
            ticker="AAPL",
            name="Apple Inc",
            instrument_type=InstrumentType.stock,
            exchange="NASDAQ",
        )
    )
    service._remember(
        InstrumentMetadata(
            ticker="AAPL34",
            name="Apple BDR",
            instrument_type=InstrumentType.bdr,
            exchange="B3",
            underlying_ticker="AAPL",
        )
    )
    assert (await service.search("aapl")).results[0].ticker == "AAPL"
    assert (await service.search("aapl")).results[1].ticker == "AAPL34"
    assert (await service.search(" ")).unavailable_reason == "A non-empty search query is required"

    class Empty:
        async def ticker_directory(self) -> list[object]:
            return []

        async def directory(self) -> list[InstrumentMetadata]:
            return [
                InstrumentMetadata(
                    ticker="PETR4", instrument_type=InstrumentType.stock, exchange="B3"
                )
            ]

    warmed = InstrumentDataService(settings, sec=Empty(), brapi_directory=Empty())  # type: ignore[arg-type]
    await warmed.warm_directory()
    await warmed.warm_directory()
    await warmed.refresh_directory()


@pytest.mark.asyncio
async def test_directory_warning_and_shutdown_cancellation_paths() -> None:
    class FailingSec:
        async def ticker_directory(self) -> list[object]:
            raise RuntimeError("offline")

    class EmptyBrapi:
        last_error = "offline"

        async def directory(self) -> list[InstrumentMetadata]:
            return []

    service = InstrumentDataService(_settings(), sec=FailingSec(), brapi_directory=EmptyBrapi())  # type: ignore[arg-type]
    await service.warm_directory()
    warning = await service.search("aapl")
    assert (
        warning.unavailable_reason and "sec_edgar_tickers unavailable" in warning.unavailable_reason
    )

    class FailingBrapi:
        async def directory(self) -> list[InstrumentMetadata]:
            raise RuntimeError("offline")

    service = InstrumentDataService(_settings(), sec=FailingSec(), brapi_directory=FailingBrapi())  # type: ignore[arg-type]
    await service.warm_directory()
    unavailable = await service.search("aapl")
    assert (
        unavailable.unavailable_reason
        and "brapi_directory_complementary unavailable" in unavailable.unavailable_reason
    )

    class Slow:
        async def ticker_directory(self) -> list[object]:
            await asyncio.sleep(10)
            return []

        async def directory(self) -> list[InstrumentMetadata]:
            await asyncio.sleep(10)
            return []

    slow = InstrumentDataService(_settings(), sec=Slow(), brapi_directory=Slow())  # type: ignore[arg-type]
    warm_task = asyncio.create_task(slow.warm_directory())
    await asyncio.sleep(0)
    await slow.close()
    await asyncio.gather(warm_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_directory_provider_fallback_and_sec_dict_shape() -> None:
    class Sec:
        async def directory(self) -> list[dict[str, object]]:
            return [{"ticker": "AAPL", "name": "Apple", "cik_str": "1"}]

    class Brapi:
        async def instruments(self) -> list[InstrumentMetadata]:
            return []

    service = InstrumentDataService(_settings(), sec=Sec(), brapi_directory=Brapi())  # type: ignore[arg-type]
    await service.warm_directory()
    found = await service.search("apple")
    assert found.results[0].identifiers["cik"] == "1"
    assert (
        _metadata_from_sec(
            InstrumentMetadata(ticker="MSFT", instrument_type=InstrumentType.stock)
        ).ticker
        == "MSFT"
    )

    with pytest.raises(RuntimeError):
        await _invoke_directory(object(), "directory", "instruments")
