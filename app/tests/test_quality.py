from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError

from app.api.dependencies import get_quality_facts_service
from app.main import create_app
from app.models import (
    FinancialPeriod,
    FundAllocation,
    FundamentalsSnapshot,
    FundDistribution,
    FundHolding,
    FundMonthlyReport,
    FundProfile,
    FundReportSeries,
    InstrumentDataResponse,
    InstrumentMetadata,
    InstrumentType,
    InternationalFundamentals,
    OpportunityMetric,
    OpportunityMetrics,
    OpportunityResponse,
)
from app.models.quality import (
    QualityAssetKind,
    QualityAssetRequest,
    QualityFactsRequest,
    QualityFactsResponse,
)
from app.services.quality import QualityFactsService

TODAY = date(2026, 7, 30)
NOW = datetime(2026, 7, 30, tzinfo=UTC)


class FundamentalsStub:
    def __init__(self, snapshot: FundamentalsSnapshot) -> None:
        self.value = snapshot
        self.calls: list[str] = []

    async def snapshot(
        self,
        ticker: str,
        *_args: object,
        **_kwargs: object,
    ) -> FundamentalsSnapshot:
        self.calls.append(ticker)
        return self.value


class InstrumentsStub:
    def __init__(self, values: dict[str, InstrumentDataResponse]) -> None:
        self.values = values

    async def get(
        self,
        ticker: str,
        _instrument_type: InstrumentType | None = None,
    ) -> InstrumentDataResponse:
        return self.values[ticker]


class OpportunityStub:
    def __init__(self, values: dict[str, OpportunityResponse]) -> None:
        self.values = values

    async def opportunity(self, ticker: str) -> OpportunityResponse:
        return self.values[ticker]


def metric(value: str | None = None) -> OpportunityMetric:
    return OpportunityMetric(
        value=Decimal(value) if value is not None else None,
        as_of=TODAY if value is not None else None,
        sources=["public"] if value is not None else [],
        unavailable_reason=None if value is not None else "missing",
    )


def opportunity_metrics() -> OpportunityMetrics:
    return OpportunityMetrics(
        current_price=metric("10"),
        shares_outstanding=metric("100"),
        earnings_per_share=metric("1"),
        book_value_per_share=metric("5"),
        price_to_book=metric("2"),
        price_to_earnings=metric("10"),
        dividend_yield_12m=metric("0.06"),
        dividends_12m=metric("0.6"),
        graham_price=metric(),
        bazin_price=metric(),
        min_52_weeks=metric("8"),
        max_52_weeks=metric("12"),
    )


def instrument_data(
    ticker: str,
    instrument: InstrumentMetadata,
    *,
    fund_profile: FundProfile | None = None,
    fundamentals: InternationalFundamentals | None = None,
) -> InstrumentDataResponse:
    return InstrumentDataResponse(
        ticker=ticker,
        instrument=instrument,
        fund_profile=fund_profile,
        fundamentals=fundamentals,
        refreshed_at=NOW,
    )


def opportunity(
    ticker: str,
    instrument: InstrumentMetadata,
    *,
    reports: list[FundMonthlyReport] | None = None,
    distributions: list[FundDistribution] | None = None,
) -> OpportunityResponse:
    return OpportunityResponse(
        ticker=ticker,
        instrument=instrument,
        metrics=opportunity_metrics(),
        fund_reports=FundReportSeries(cnpj="123", reports=reports or []),
        fund_distributions=distributions or [],
        refreshed_at=NOW,
    )


def annual_period(year: int, revenue: str, shares: str) -> FinancialPeriod:
    value = Decimal(revenue)
    return FinancialPeriod(
        period_end=date(year, 12, 31),
        consolidated=True,
        annual=True,
        revenue=value,
        gross_profit=value * Decimal("0.45"),
        ebit=value * Decimal("0.20"),
        ebitda=value * Decimal("0.24"),
        financial_result=value * Decimal("-0.03"),
        net_income=value * Decimal("0.12"),
        equity=value * Decimal("0.60"),
        total_assets=value,
        current_assets=value * Decimal("0.40"),
        current_liabilities=value * Decimal("0.20"),
        operating_cash_flow=value * Decimal("0.15"),
        free_cash_flow=value * Decimal("0.10"),
        net_debt=value * Decimal("0.12"),
        shares_outstanding=Decimal(shares),
    )


def stock_snapshot() -> FundamentalsSnapshot:
    periods = [
        annual_period(2022, "800", "100"),
        annual_period(2023, "900", "101"),
        annual_period(2024, "1000", "102"),
    ]
    return FundamentalsSnapshot(
        ticker="TEST3",
        cnpj="11111111000111",
        company_name="Test",
        periods=periods,
        trailing_twelve_months=periods[-1],
    )


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        (QualityAssetKind.crypto, "network-data"),
        (QualityAssetKind.fixed_income, "issuer and instrument"),
    ],
)
async def test_reports_specialized_sources_for_unsupported_local_kinds(
    kind: QualityAssetKind,
    reason: str,
) -> None:
    service = QualityFactsService(
        FundamentalsStub(stock_snapshot()),  # type: ignore[arg-type]
        InstrumentsStub({}),  # type: ignore[arg-type]
        OpportunityStub({}),  # type: ignore[arg-type]
    )

    response = await service.resolve(
        QualityFactsRequest(assets=[QualityAssetRequest(ticker="btc", kind=kind)])
    )

    assert response.assets[0].ticker == "BTC"
    assert reason in (response.assets[0].unavailable_reason or "")


def test_request_normalizes_tickers_and_rejects_duplicates() -> None:
    request = QualityFactsRequest(
        assets=[QualityAssetRequest(ticker=" abtc11 ", kind=QualityAssetKind.etf)]
    )

    assert request.assets[0].ticker == "ABTC11"
    with pytest.raises(ValidationError, match="duplicates"):
        QualityFactsRequest(
            assets=[
                QualityAssetRequest(ticker="ABTC11", kind=QualityAssetKind.etf),
                QualityAssetRequest(ticker="abtc11", kind=QualityAssetKind.etf),
            ]
        )


async def test_resolves_complete_domestic_stock_quality_facts() -> None:
    instrument = InstrumentMetadata(
        ticker="TEST3",
        name="Test",
        instrument_type=InstrumentType.stock,
        category="SHARES",
        isin="BRTESTACNOR0",
    )
    fundamentals = FundamentalsStub(stock_snapshot())
    service = QualityFactsService(
        fundamentals,  # type: ignore[arg-type]
        InstrumentsStub({"TEST3": instrument_data("TEST3", instrument)}),  # type: ignore[arg-type]
        OpportunityStub({"TEST3": opportunity("TEST3", instrument)}),  # type: ignore[arg-type]
    )

    response = await service.resolve(
        QualityFactsRequest(
            assets=[QualityAssetRequest(ticker="TEST3", kind=QualityAssetKind.stock)]
        )
    )

    result = response.assets[0]
    facts = {fact.key: fact for fact in result.facts}
    assert result.canonical_id == "11111111000111"
    assert facts["gross_margin"].value == Decimal("0.45")
    assert facts["current_ratio"].value == Decimal("2")
    assert facts["interest_coverage"].value == Decimal("6.666666666666666666666666667")
    assert facts["positive_earnings_frequency"].value == Decimal("1")
    assert facts["revenue_cagr"].value is not None
    assert facts["share_dilution"].value is not None
    assert all(fact.source == "cvm" for fact in result.facts if fact.value is not None)
    assert fundamentals.calls == ["TEST3"]


async def test_financial_facts_preserve_missing_history_and_zero_denominator() -> None:
    period = annual_period(2024, "1000", "100").model_copy(
        update={
            "revenue": Decimal("0"),
            "financial_result": Decimal("20"),
        }
    )
    snapshot = FundamentalsSnapshot(
        ticker="MISS3",
        cnpj="222",
        periods=[period],
        trailing_twelve_months=period,
    )
    instrument = InstrumentMetadata(
        ticker="MISS3",
        name="Missing",
        instrument_type=InstrumentType.stock,
        category="SHARES",
    )
    service = QualityFactsService(
        FundamentalsStub(snapshot),  # type: ignore[arg-type]
        InstrumentsStub({"MISS3": instrument_data("MISS3", instrument)}),  # type: ignore[arg-type]
        OpportunityStub({"MISS3": opportunity("MISS3", instrument)}),  # type: ignore[arg-type]
    )

    response = await service.resolve(
        QualityFactsRequest(
            assets=[QualityAssetRequest(ticker="MISS3", kind=QualityAssetKind.stock)]
        )
    )

    facts = {fact.key: fact for fact in response.assets[0].facts}
    assert facts["gross_margin"].status == "missing_data"
    assert facts["interest_coverage"].value is None
    assert facts["revenue_cagr"].unavailable_reason == (
        "At least three annual observations are required"
    )


async def test_reports_stock_snapshot_resolution_failure() -> None:
    instrument = InstrumentMetadata(
        ticker="MISS3",
        name="Missing",
        instrument_type=InstrumentType.stock,
        category="SHARES",
    )
    snapshot = FundamentalsSnapshot(ticker="MISS3", unavailable_reason="No filing")
    service = QualityFactsService(
        FundamentalsStub(snapshot),  # type: ignore[arg-type]
        InstrumentsStub({"MISS3": instrument_data("MISS3", instrument)}),  # type: ignore[arg-type]
        OpportunityStub({"MISS3": opportunity("MISS3", instrument)}),  # type: ignore[arg-type]
    )

    response = await service.resolve(
        QualityFactsRequest(
            assets=[QualityAssetRequest(ticker="MISS3", kind=QualityAssetKind.stock)]
        )
    )

    assert response.assets[0].unavailable_reason == "No filing"


async def test_resolves_international_stock_public_facts() -> None:
    instrument = InstrumentMetadata(
        ticker="ACME",
        instrument_type=InstrumentType.stock,
        category="INTERNATIONAL",
        isin="US0000000001",
    )
    data = instrument_data(
        "ACME",
        instrument,
        fundamentals=InternationalFundamentals(
            market_capitalization=Decimal("5000000"),
            dividend_yield=Decimal("0.02"),
            source="public_filings",
        ),
    )
    service = QualityFactsService(
        FundamentalsStub(stock_snapshot()),  # type: ignore[arg-type]
        InstrumentsStub({"ACME": data}),  # type: ignore[arg-type]
        OpportunityStub({"ACME": opportunity("ACME", instrument)}),  # type: ignore[arg-type]
    )

    response = await service.resolve(
        QualityFactsRequest(
            assets=[QualityAssetRequest(ticker="ACME", kind=QualityAssetKind.stock)]
        )
    )

    result = response.assets[0]
    assert result.canonical_id == "US0000000001"
    assert {fact.key for fact in result.facts} == {
        "market_capitalization",
        "dividend_yield",
    }
    assert result.warnings == ["Public international profitability history is unavailable"]


async def test_reports_missing_international_fundamentals() -> None:
    instrument = InstrumentMetadata(
        ticker="EMPTY",
        instrument_type=InstrumentType.stock,
        category="INTERNATIONAL",
    )
    service = QualityFactsService(
        FundamentalsStub(stock_snapshot()),  # type: ignore[arg-type]
        InstrumentsStub({"EMPTY": instrument_data("EMPTY", instrument)}),  # type: ignore[arg-type]
        OpportunityStub({"EMPTY": opportunity("EMPTY", instrument)}),  # type: ignore[arg-type]
    )

    response = await service.resolve(
        QualityFactsRequest(
            assets=[QualityAssetRequest(ticker="EMPTY", kind=QualityAssetKind.stock)]
        )
    )

    assert "international fundamentals" in (response.assets[0].unavailable_reason or "")


async def test_resolves_etf_cost_scale_and_diversification_facts() -> None:
    instrument = InstrumentMetadata(
        ticker="ETF",
        instrument_type=InstrumentType.etf,
        category="INTERNATIONAL",
        isin="IE0000000001",
    )
    profile = FundProfile(
        net_assets=Decimal("1000000000"),
        net_expense_ratio=Decimal("0.001"),
        portfolio_turnover=Decimal("0.12"),
        inception_date=date(2016, 7, 30),
        holdings=[
            FundHolding(symbol="A", weight=Decimal("40")),
            FundHolding(symbol="B", weight=Decimal("35")),
            FundHolding(symbol="C", weight=Decimal("25")),
        ],
        sectors=[
            FundAllocation(name="Technology", weight=Decimal("0.6")),
            FundAllocation(name="Finance", weight=Decimal("0.4")),
        ],
        source="public_fund_profile",
    )
    service = QualityFactsService(
        FundamentalsStub(stock_snapshot()),  # type: ignore[arg-type]
        InstrumentsStub({"ETF": instrument_data("ETF", instrument, fund_profile=profile)}),  # type: ignore[arg-type]
        OpportunityStub({"ETF": opportunity("ETF", instrument)}),  # type: ignore[arg-type]
    )

    response = await service.resolve(
        QualityFactsRequest(assets=[QualityAssetRequest(ticker="ETF", kind=QualityAssetKind.etf)])
    )

    facts = {fact.key: fact.value for fact in response.assets[0].facts}
    assert facts["holdings_count"] == Decimal("3")
    assert facts["top_ten_concentration"] == Decimal("100")
    assert facts["holdings_hhi"] == Decimal("0.3450")
    assert facts["sector_hhi"] == Decimal("0.52")
    assert facts["fund_age_years"] is not None


async def test_reports_missing_etf_profile() -> None:
    instrument = InstrumentMetadata(ticker="ETF", instrument_type=InstrumentType.etf)
    service = QualityFactsService(
        FundamentalsStub(stock_snapshot()),  # type: ignore[arg-type]
        InstrumentsStub({"ETF": instrument_data("ETF", instrument)}),  # type: ignore[arg-type]
        OpportunityStub({"ETF": opportunity("ETF", instrument)}),  # type: ignore[arg-type]
    )

    response = await service.resolve(
        QualityFactsRequest(assets=[QualityAssetRequest(ticker="ETF", kind=QualityAssetKind.etf)])
    )

    assert response.assets[0].unavailable_reason == "No public fund profile was resolved"


async def test_resolves_fund_reporting_and_distribution_stability() -> None:
    instrument = InstrumentMetadata(
        ticker="FUND11",
        instrument_type=InstrumentType.fii,
        isin="BRFUNDCTF001",
    )
    reports = [
        FundMonthlyReport(as_of=date(2026, month, 1), nav_per_share=Decimal("100"))
        for month in range(1, 7)
    ]
    distributions = [
        FundDistribution(ex_date=date(2026, month, 15), value=Decimal("1"), source="cvm")
        for month in range(1, 7)
    ]
    service = QualityFactsService(
        FundamentalsStub(stock_snapshot()),  # type: ignore[arg-type]
        InstrumentsStub({"FUND11": instrument_data("FUND11", instrument)}),  # type: ignore[arg-type]
        OpportunityStub(
            {
                "FUND11": opportunity(
                    "FUND11",
                    instrument,
                    reports=reports,
                    distributions=distributions,
                )
            }
        ),  # type: ignore[arg-type]
    )

    response = await service.resolve(
        QualityFactsRequest(
            assets=[
                QualityAssetRequest(
                    ticker="FUND11",
                    kind=QualityAssetKind.real_estate_fund,
                )
            ]
        )
    )

    result = response.assets[0]
    facts = {fact.key: fact.value for fact in result.facts}
    assert result.canonical_id == "BRFUNDCTF001"
    assert facts["reporting_history_months"] == Decimal("6")
    assert facts["distribution_stability"] == Decimal("0")
    assert facts["positive_distribution_frequency"] == Decimal("1")


async def test_fund_with_short_history_explains_missing_stability() -> None:
    instrument = InstrumentMetadata(ticker="FUND11", instrument_type=InstrumentType.fii)
    service = QualityFactsService(
        FundamentalsStub(stock_snapshot()),  # type: ignore[arg-type]
        InstrumentsStub({"FUND11": instrument_data("FUND11", instrument)}),  # type: ignore[arg-type]
        OpportunityStub(
            {
                "FUND11": opportunity(
                    "FUND11",
                    instrument,
                    distributions=[
                        FundDistribution(
                            ex_date=TODAY,
                            value=Decimal("1"),
                            source="cvm",
                        )
                    ],
                )
            }
        ),  # type: ignore[arg-type]
    )

    response = await service.resolve(
        QualityFactsRequest(
            assets=[
                QualityAssetRequest(
                    ticker="FUND11",
                    kind=QualityAssetKind.real_estate_fund,
                )
            ]
        )
    )

    facts = {fact.key: fact for fact in response.assets[0].facts}
    assert facts["distribution_stability"].status == "missing_data"
    assert "six distributions" in (facts["distribution_stability"].unavailable_reason or "")


async def test_quality_endpoint_uses_bounded_service_contract() -> None:
    class EndpointStub:
        async def resolve(self, request: QualityFactsRequest) -> QualityFactsResponse:
            service = QualityFactsService(
                FundamentalsStub(stock_snapshot()),  # type: ignore[arg-type]
                InstrumentsStub({}),  # type: ignore[arg-type]
                OpportunityStub({}),  # type: ignore[arg-type]
            )
            return await service.resolve(request)

    app = create_app()
    app.dependency_overrides[get_quality_facts_service] = lambda: EndpointStub()
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/quality/facts:resolve",
            json={"assets": [{"ticker": "BTC", "kind": "crypto"}]},
        )

    assert response.status_code == 200
    assert response.json()["assets"][0]["kind"] == "crypto"
    assert response.headers["cache-control"].startswith("private")
