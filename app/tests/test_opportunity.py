from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.models import (
    AssetDetails,
    AssetResponse,
    DetailSection,
    Dividend,
    FieldData,
    FundDistribution,
    InstrumentBatchRequest,
    InstrumentMetadata,
    InstrumentType,
)
from app.scrapers.cvm_fund_reports import FundReportPoint, FundReportSeries
from app.services.opportunity import (
    B3InstrumentProvider,
    OpportunityService,
    StatusInvestProfile,
    StatusInvestProvider,
    parse_status_invest_profile,
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
            shares_count=Decimal("1000"),
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
    def cached(self, tickers: list[str]) -> list[InstrumentMetadata]:
        return [self._instrument(ticker) for ticker in tickers]

    async def get(self, ticker: str) -> InstrumentMetadata:
        return self._instrument(ticker)

    @staticmethod
    def _instrument(ticker: str) -> InstrumentMetadata:
        return InstrumentMetadata(
            ticker=ticker,
            name="Example",
            instrument_type=InstrumentType.stock,
        )


@pytest.mark.asyncio
async def test_opportunity_service_resolves_instruments_from_cache_without_upstream_io() -> None:
    class CachedB3Provider(FakeB3Provider):
        get_calls = 0

        async def get(self, ticker: str) -> InstrumentMetadata:
            self.get_calls += 1
            return await super().get(ticker)

        def cached(self, tickers: list[str]) -> list[InstrumentMetadata]:
            return [self._instrument(ticker) for ticker in tickers if ticker != "MISS3"]

    provider = CachedB3Provider()

    service = OpportunityService(
        FakeAssetService(),  # type: ignore[arg-type]
        Settings(),
        b3_provider=provider,  # type: ignore[arg-type]
    )

    resolved = await service.instruments(["PETR4", "MISS3", "VALE3"])

    assert [instrument.ticker for instrument in resolved] == ["PETR4", "VALE3"]
    assert provider.get_calls == 0


@pytest.mark.asyncio
async def test_instrument_batch_endpoint_normalizes_and_deduplicates_tickers() -> None:
    from app.api.dependencies import get_opportunity_service
    from app.main import create_app

    class StubOpportunityService:
        requested: list[str] = []

        async def instruments(self, tickers: list[str]) -> list[InstrumentMetadata]:
            self.requested = tickers
            return [
                InstrumentMetadata(
                    ticker=ticker,
                    instrument_type=InstrumentType.stock,
                )
                for ticker in tickers
            ]

    service = StubOpportunityService()
    app = create_app()
    app.dependency_overrides[get_opportunity_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/v1/instruments:resolve",
            json={"tickers": ["petr4", "PETR4", "vale3"]},
        )

    assert response.status_code == 200
    assert service.requested == ["PETR4", "VALE3"]
    assert [item["ticker"] for item in response.json()["instruments"]] == ["PETR4", "VALE3"]


def test_instrument_batch_request_rejects_invalid_tickers() -> None:
    with pytest.raises(ValidationError, match="invalid ticker"):
        InstrumentBatchRequest(tickers=["bad ticker"])


class FakeStatusProvider:
    async def get(self, ticker: str, instrument_type: InstrumentType | None) -> dict[str, Decimal]:
        return {"dividend_yield_12m": Decimal("10")}

    async def profile(
        self,
        ticker: str,
        instrument_type: InstrumentType | None,
    ) -> StatusInvestProfile:
        return StatusInvestProfile(values=await self.get(ticker, instrument_type))


class FakeEmptyStatusProvider:
    async def get(
        self,
        ticker: str,
        instrument_type: InstrumentType | None,
    ) -> dict[str, Decimal]:
        return {}

    async def profile(
        self,
        ticker: str,
        instrument_type: InstrumentType | None,
    ) -> StatusInvestProfile:
        return StatusInvestProfile(values={})


class FakeCvmProvider:
    async def reports(
        self,
        instrument: InstrumentMetadata | None,
        *,
        cnpj: str | None = None,
        today: date | None = None,
    ) -> FundReportSeries:
        return FundReportSeries(
            cnpj=cnpj,
            reports=(
                FundReportPoint(
                    as_of=date(2026, 6, 1),
                    nav_per_share=Decimal("40"),
                    monthly_distribution_yield=Decimal("0.01"),
                ),
            ),
        )


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
    assert result.metrics.shares_outstanding.value == Decimal("1000")
    assert result.metrics.earnings_per_share.value == Decimal("2")
    assert result.metrics.book_value_per_share.value == Decimal("20")
    assert result.metrics.dividend_yield_12m.value == Decimal("10")
    assert result.metrics.bazin_price.value == Decimal("50")
    assert result.metrics.graham_price.value is not None


@pytest.mark.asyncio
async def test_opportunity_reports_zero_for_a_confirmed_non_dividend_payer() -> None:
    class NoDividendAssetService(FakeAssetService):
        async def get_asset(self, ticker: str) -> AssetResponse:
            asset = await super().get_asset(ticker)
            return asset.model_copy(update={"dividends": []})

    service = OpportunityService(
        NoDividendAssetService(),  # type: ignore[arg-type]
        Settings(),
        b3_provider=FakeB3Provider(),  # type: ignore[arg-type]
        status_provider=FakeEmptyStatusProvider(),  # type: ignore[arg-type]
    )

    result = await service.opportunity("TEST3")

    assert result.metrics.dividends_12m.value == Decimal("0")
    assert result.metrics.dividend_yield_12m.value == Decimal("0")
    assert result.metrics.bazin_price.value == Decimal("0")


@pytest.mark.asyncio
async def test_opportunity_recomputes_yield_from_reconciled_dividends_and_price() -> None:
    class ConflictingYieldAssetService(FakeAssetService):
        async def get_asset(self, ticker: str) -> AssetResponse:
            asset = await super().get_asset(ticker)
            assert asset.details is not None
            details = asset.details.model_copy(
                update={
                    "sections": [
                        *asset.details.sections,
                        DetailSection(
                            name="Yield",
                            key_normalized="yield",
                            fields=[
                                FieldData(
                                    label="Div. Yield",
                                    key_normalized="div_yield",
                                    value=Decimal("1"),
                                    raw_value="1,00%",
                                    value_type="percent",
                                )
                            ],
                        ),
                    ]
                }
            )
            return asset.model_copy(update={"details": details})

    service = OpportunityService(
        ConflictingYieldAssetService(),  # type: ignore[arg-type]
        Settings(),
        b3_provider=FakeB3Provider(),  # type: ignore[arg-type]
        status_provider=FakeStatusProvider(),  # type: ignore[arg-type]
    )

    result = await service.opportunity("TEST3")

    assert result.metrics.dividends_12m.value == Decimal("3")
    assert result.metrics.dividend_yield_12m.value == Decimal("10.0")


@pytest.mark.asyncio
async def test_fund_opportunity_prefers_official_nav_and_exposes_income_horizons() -> None:
    class FundB3Provider(FakeB3Provider):
        async def get(self, ticker: str) -> InstrumentMetadata:
            return InstrumentMetadata(
                ticker=ticker,
                name="Example FII",
                instrument_type=InstrumentType.fii,
                isin="BREXAMCTF000",
            )

    class FundStatusProvider(FakeStatusProvider):
        async def profile(
            self,
            ticker: str,
            instrument_type: InstrumentType | None,
        ) -> StatusInvestProfile:
            return StatusInvestProfile(
                values=await self.get(ticker, instrument_type),
                cnpj="12.345.678/0001-00",
                distributions=(
                    FundDistribution(
                        ex_date=date(2026, 6, 30),
                        value=Decimal("1.20"),
                        source="status_invest",
                    ),
                    FundDistribution(
                        ex_date=date(2026, 5, 30),
                        value=Decimal("1.00"),
                        source="status_invest",
                    ),
                    FundDistribution(
                        ex_date=date(2026, 4, 30),
                        value=Decimal("0.80"),
                        source="status_invest",
                    ),
                ),
            )

    service = OpportunityService(
        FakeAssetService(),  # type: ignore[arg-type]
        Settings(),
        b3_provider=FundB3Provider(),  # type: ignore[arg-type]
        status_provider=FundStatusProvider(),  # type: ignore[arg-type]
        cvm_provider=FakeCvmProvider(),  # type: ignore[arg-type]
    )

    result = await service.opportunity("TEST11")

    assert result.metrics.book_value_per_share.value == Decimal("40")
    assert result.metrics.book_value_per_share.sources == ["cvm"]
    assert result.metrics.price_to_book.value == Decimal("0.75")
    assert result.metrics.latest_distribution is not None
    assert result.metrics.latest_distribution.value == Decimal("1.20")
    assert result.metrics.median_distribution_3m is not None
    assert result.metrics.median_distribution_3m.value == Decimal("1.20")
    assert result.fund_reports is not None
    assert result.fund_reports.cnpj == "12.345.678/0001-00"
    assert len(result.fund_distributions) == 4


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
    <div class="item"><div><div><strong class="value">4,00</strong>
      <div><button data-key="p_l"></button></div></div></div></div>
    <div class="item"><div><div><strong class="value">1,25</strong>
      <div><button data-key="p_vp"></button></div></div></div></div>
    <div class="item"><div><div><strong class="value">2,50</strong>
      <div><button data-key="lpa"></button></div></div></div></div>
    <div class="item"><div><div><strong class="value">8,00</strong>
      <div><button data-key="vpa"></button></div></div></div></div>
    <button data-key></button>
    """

    assert parse_status_invest_snapshot(html) == {
        "current_price": Decimal("97.89"),
        "min_52_weeks": Decimal("87.93"),
        "max_52_weeks": Decimal("104.30"),
        "dividend_yield_12m": Decimal("10.47"),
        "dividends_12m": Decimal("10.25"),
        "price_to_earnings": Decimal("4.00"),
        "price_to_book": Decimal("1.25"),
        "earnings_per_share": Decimal("2.50"),
        "book_value_per_share": Decimal("8.00"),
    }


def test_status_profile_reads_cnpj_and_distribution_history() -> None:
    html = """
    <div class="info"><h3 class="title">CNPJ</h3>
      <strong class="value">42.730.834/0001-00</strong>
    </div>
    <div id="earning-section">
      <input id="results" value='[
        {"ed":"29/05/2026","et":"Rendimento","v":0.5},
        {"ed":"30/04/2026","et":"Rendimento","v":0.75},
        {"ed":"01/04/2026","et":"Amortização","v":1.25},
        {"ed":"invalid","et":"Rendimento","v":"bad"}
      ]'>
    </div>
    """

    result = parse_status_invest_profile(html)

    assert result.cnpj == "42730834000100"
    assert [(item.ex_date, item.value) for item in result.distributions] == [
        (date(2026, 5, 29), Decimal("0.5")),
        (date(2026, 4, 30), Decimal("0.75")),
    ]


def test_status_profile_reads_fi_infra_cnpj_layout() -> None:
    html = """
    <div class="fund-section-itens">
      <div>
        <strong>Cnpj</strong>
        <span class="span-item">42.730.834/0001-00</span>
      </div>
    </div>
    """

    assert parse_status_invest_profile(html).cnpj == "42730834000100"


@pytest.mark.asyncio
async def test_status_invest_provider_sends_navigation_referer() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["referer"] == "https://statusinvest.com.br/"
        return httpx.Response(
            200,
            text='<div title="Valor atual do ativo"><strong class="value">10,00</strong></div>',
        )

    provider = StatusInvestProvider(Settings(), httpx.MockTransport(handler))

    assert await provider.get("TEST3", InstrumentType.stock) == {"current_price": Decimal("10.00")}


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
    assert provider.cached(["JURO11", "MISS11"]) == [result]


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


@pytest.mark.asyncio
async def test_status_invest_cache_bounds_attacker_selected_tickers() -> None:
    provider = StatusInvestProvider(
        Settings(ticker_cache_max_entries=1),
        httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                text=(
                    '<div title="Valor atual do ativo"><strong class="value">10,00</strong></div>'
                ),
            )
        ),
    )

    await provider.get("TEST3", InstrumentType.stock)
    await provider.get("NEXT3", InstrumentType.stock)

    assert len(provider._cache) == 1
