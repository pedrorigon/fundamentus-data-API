from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest

from app.config import Settings
from app.models import (
    AssetDetails,
    AssetResponse,
    DetailSection,
    Dividend,
    FieldData,
    InstrumentMetadata,
    InstrumentType,
)
from app.services.opportunity import (
    B3InstrumentProvider,
    OpportunityService,
    StatusInvestProvider,
    parse_status_invest_snapshot,
)


class FakeAssetService:
    async def get_asset(self, ticker: str) -> AssetResponse:
        details = AssetDetails(
            ticker=ticker,
            quote=Decimal("30"),
            quote_date=date(2026, 7, 10),
            book_value_per_share=Decimal("20"),
            earnings_per_share=Decimal("2"),
            min_52_weeks=Decimal("22"),
            max_52_weeks=Decimal("35"),
            sections=[
                DetailSection(
                    name="Indicators",
                    key_normalized="indicators",
                    fields=[
                        FieldData(
                            label="P/VP",
                            key_normalized="p_vp",
                            value=Decimal("1.5"),
                            raw_value="1,50",
                            value_type="number",
                        )
                    ],
                )
            ],
            source_url="https://example.test",
            scraped_at=datetime(2026, 7, 10, tzinfo=UTC),
        )
        dividend = Dividend(
            ex_date=date(2026, 6, 1),
            payment_date=date(2026, 6, 10),
            value=Decimal("3"),
            type="Dividend",
            is_future_payment=False,
            is_future_ex_date=False,
            raw={},
        )
        return AssetResponse(ticker=ticker, details=details, dividends=[dividend])


class FakeB3Provider:
    async def get(self, ticker: str) -> InstrumentMetadata:
        return InstrumentMetadata(
            ticker=ticker,
            name="Example",
            instrument_type=InstrumentType.stock,
        )


class FakeStatusProvider:
    async def get(self, ticker: str, instrument_type: InstrumentType | None) -> dict[str, Decimal]:
        return {"dividend_yield_12m": Decimal("10")}


@pytest.mark.asyncio
async def test_opportunity_service_calculates_valuation_metrics() -> None:
    service = OpportunityService(
        FakeAssetService(),  # type: ignore[arg-type]
        Settings(),
        b3_provider=FakeB3Provider(),  # type: ignore[arg-type]
        status_provider=FakeStatusProvider(),  # type: ignore[arg-type]
    )

    result = await service.opportunity("TEST3")

    assert result.instrument is not None
    assert result.metrics.price_to_book.value == Decimal("1.5")
    assert result.metrics.price_to_earnings.value == Decimal("15")
    assert result.metrics.dividend_yield_12m.value == Decimal("10")
    assert result.metrics.bazin_price.value == Decimal("50")
    assert result.metrics.graham_price.value is not None


def test_status_invest_parser_reads_visible_opportunity_values() -> None:
    html = """
    <div title="Valor atual do ativo"><strong class="value">97,89</strong></div>
    <div title="Valor mínimo das últimas 52 semanas"><strong class="value">87,93</strong></div>
    <div title="Valor máximo das últimas 52 semanas"><strong class="value">104,30</strong></div>
    <div title="Dividend Yield com base nos últimos 12 meses">
      <strong class="value">10,47</strong>
    </div>
    <div title="Soma total de proventos distribuídos nos últimos 12 meses">
      <span class="sub-value">R$ 10,25</span>
    </div>
    """

    assert parse_status_invest_snapshot(html) == {
        "current_price": Decimal("97.89"),
        "min_52_weeks": Decimal("87.93"),
        "max_52_weeks": Decimal("104.30"),
        "dividend_yield_12m": Decimal("10.47"),
        "dividends_12m": Decimal("10.25"),
    }


@pytest.mark.asyncio
async def test_b3_provider_classifies_juro11_as_infrastructure_fund() -> None:
    payload = {
        "table": {
            "columns": [
                {"name": "RptDt"},
                {"name": "TckrSymb"},
                {"name": "SgmtNm"},
                {"name": "SctyCtgyNm"},
                {"name": "CrpnNm"},
                {"name": "CFICd"},
                {"name": "ISIN"},
                {"name": "TradgCcy"},
            ],
            "values": [
                [
                    "2026-07-10T00:00:00",
                    "JURO11",
                    "CASH",
                    "FUNDS",
                    "SPARTA INFRA FIC FI INFRA RENDA FIXA CP",
                    "CFCGIU",
                    "BRJUROCTF002",
                    "BRL",
                ]
            ],
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    provider = B3InstrumentProvider(Settings(), httpx.MockTransport(handler))
    result = await provider.get("juro11")

    assert result is not None
    assert result.instrument_type is InstrumentType.fi_infra
    assert result.name == "SPARTA INFRA FIC FI INFRA RENDA FIXA CP"
    assert result.source == "b3"


@pytest.mark.asyncio
async def test_b3_provider_rejects_invalid_ticker_without_network() -> None:
    def unexpected_request(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("network should not be called")

    provider = B3InstrumentProvider(Settings(), httpx.MockTransport(unexpected_request))

    with pytest.raises(Exception, match="Invalid ticker"):
        await provider.get("bad ticker")


@pytest.mark.asyncio
async def test_external_opportunity_providers_cache_successful_responses() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            text='<div title="Valor atual do ativo"><strong class="value">10,00</strong></div>',
        )

    provider = StatusInvestProvider(Settings(), httpx.MockTransport(handler))

    first = await provider.get("TEST3", InstrumentType.stock)
    second = await provider.get("TEST3", InstrumentType.stock)

    assert first == second == {"current_price": Decimal("10.00")}
    assert calls == 1
