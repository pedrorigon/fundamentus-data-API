from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError

from app.api.dependencies import get_quality_facts_service
from app.core.errors import InvalidTickerError
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
    SectorCompany,
)
from app.models.quality import (
    QualityAssetKind,
    QualityAssetRequest,
    QualityFactsRequest,
    QualityFactsResponse,
)
from app.services.bcb_quality import BankQualitySnapshot, MacroQualitySnapshot
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
        average_daily_traded_value=metric("50000000"),
        market_capitalization=metric("50000000000"),
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
        annual_period(2021, "700", "99"),
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
        shares_outstanding=Decimal("100"),
        earnings_per_share=Decimal("1.2"),
        book_value_per_share=Decimal("6"),
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


async def test_isolates_provider_errors_inside_a_quality_batch() -> None:
    class FailingInstruments:
        async def get(
            self,
            ticker: str,
            _instrument_type: InstrumentType | None = None,
        ) -> InstrumentDataResponse:
            raise InvalidTickerError(ticker=ticker)

    service = QualityFactsService(
        FundamentalsStub(stock_snapshot()),  # type: ignore[arg-type]
        FailingInstruments(),  # type: ignore[arg-type]
        OpportunityStub({}),  # type: ignore[arg-type]
    )

    response = await service.resolve(
        QualityFactsRequest(
            assets=[
                QualityAssetRequest(ticker="VUAA.L", kind=QualityAssetKind.etf),
                QualityAssetRequest(ticker="BTC", kind=QualityAssetKind.crypto),
            ]
        )
    )

    assert response.assets[0].unavailable_reason == "Invalid ticker."
    assert "network-data" in (response.assets[1].unavailable_reason or "")


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
    assert facts["operating_cash_flow_margin"].value == Decimal("0.15")
    assert facts["accrual_ratio"].value == Decimal("-0.03")
    assert facts["earnings_per_share_consistency_error"].value == Decimal("0")
    assert facts["book_value_per_share_consistency_error"].value == Decimal("0")
    assert result.profile == "industrial"
    assert fundamentals.calls == ["TEST3"]


async def test_resolves_bank_specific_macro_peer_and_prudential_facts() -> None:
    sector = "Intermediários Financeiros"
    snapshot = stock_snapshot().model_copy(
        update={
            "ticker": "ITUB4",
            "company_name": "ITAU UNIBANCO HOLDING S.A.",
            "sector": sector,
        }
    )

    class FundamentalsWithPeers(FundamentalsStub):
        async def sector_universe(self) -> dict[str, list[SectorCompany]]:
            peer_periods = [
                annual_period(2024, revenue, "100") for revenue in ("800", "900", "1000")
            ]
            return {
                sector: [
                    SectorCompany(
                        cnpj=str(index),
                        company_name=f"Peer {index}",
                        sector=sector,
                        period=period,
                    )
                    for index, period in enumerate(peer_periods)
                ]
            }

    class MacroStub:
        async def snapshot(self) -> MacroQualitySnapshot:
            return MacroQualitySnapshot(
                inflation_by_year={
                    2022: Decimal("0.058"),
                    2023: Decimal("0.045"),
                    2024: Decimal("0.048"),
                },
                selic_by_year={
                    2021: Decimal("0.07"),
                    2022: Decimal("0.12"),
                    2023: Decimal("0.13"),
                    2024: Decimal("0.11"),
                },
                as_of=TODAY,
            )

    class BankStub:
        async def snapshot(self, company_name: str) -> BankQualitySnapshot:
            assert company_name == "ITAU UNIBANCO HOLDING S.A."
            return BankQualitySnapshot(
                basel_ratio=Decimal("0.147697"),
                core_capital_ratio=Decimal("0.119663"),
                leverage_ratio=Decimal("0.064760"),
                high_risk_credit_ratio=Decimal("0.032"),
                capital_as_of=date(2026, 3, 1),
                credit_as_of=date(2024, 12, 1),
            )

    instrument = InstrumentMetadata(
        ticker="ITUB4",
        name="ITAU UNIBANCO HOLDING S.A.",
        instrument_type=InstrumentType.stock,
        category="SHARES",
        isin="BRITUBACNPR1",
    )
    distributions = [
        FundDistribution(
            ex_date=date(year, 6, 15),
            value=Decimal("0.10"),
            source="b3",
        )
        for year in range(2022, 2025)
    ]
    service = QualityFactsService(
        FundamentalsWithPeers(snapshot),  # type: ignore[arg-type]
        InstrumentsStub({"ITUB4": instrument_data("ITUB4", instrument)}),  # type: ignore[arg-type]
        OpportunityStub(
            {
                "ITUB4": opportunity(
                    "ITUB4",
                    instrument,
                    distributions=distributions,
                )
            }
        ),  # type: ignore[arg-type]
        macro_provider=MacroStub(),  # type: ignore[arg-type]
        bank_provider=BankStub(),  # type: ignore[arg-type]
    )

    result = (
        await service.resolve(
            QualityFactsRequest(
                assets=[QualityAssetRequest(ticker="ITUB4", kind=QualityAssetKind.stock)]
            )
        )
    ).assets[0]
    facts = {fact.key: fact for fact in result.facts}

    assert result.profile == "bank"
    assert facts["basel_ratio"].value == Decimal("0.147697")
    assert facts["high_risk_credit_ratio"].value == Decimal("0.032")
    assert facts["roe_vs_sector_median"].value is not None
    assert facts["roe_vs_selic_spread"].value is not None
    assert facts["revenue_real_cagr"].value is not None
    assert facts["daily_traded_value"].value == Decimal("50000000")
    assert facts["market_capitalization"].value == Decimal("50000000000")
    assert "bcb_ifdata" in result.sources


@pytest.mark.parametrize(
    ("sector", "company_name", "expected_profile"),
    [
        ("Holdings Diversificadas", "BB SEGURIDADE PARTICIPACOES S.A.", "insurer"),
        ("Energia Elétrica", "Electric Company", "utility"),
        ("Petróleo, Gás e Biocombustíveis", "Oil Company", "commodity"),
    ],
)
async def test_classifies_stock_methodology_from_registered_sector(
    sector: str,
    company_name: str,
    expected_profile: str,
) -> None:
    snapshot = stock_snapshot().model_copy(update={"sector": sector, "company_name": company_name})
    instrument = InstrumentMetadata(
        ticker="SECT3",
        name="Sector Company",
        instrument_type=InstrumentType.stock,
        category="SHARES",
    )
    service = QualityFactsService(
        FundamentalsStub(snapshot),  # type: ignore[arg-type]
        InstrumentsStub({"SECT3": instrument_data("SECT3", instrument)}),  # type: ignore[arg-type]
        OpportunityStub({"SECT3": opportunity("SECT3", instrument)}),  # type: ignore[arg-type]
    )

    result = (
        await service.resolve(
            QualityFactsRequest(
                assets=[QualityAssetRequest(ticker="SECT3", kind=QualityAssetKind.stock)]
            )
        )
    ).assets[0]

    assert result.profile == expected_profile


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


async def test_financial_facts_reject_implausible_cross_checked_growth() -> None:
    periods = [
        annual_period(2022, "100", "100"),
        annual_period(2023, "200", "100"),
        annual_period(2024, "1000", "300"),
    ]
    snapshot = FundamentalsSnapshot(
        ticker="OUT3",
        sector="Bens Industriais",
        periods=periods,
        trailing_twelve_months=periods[-1],
    )
    instrument = InstrumentMetadata(
        ticker="OUT3",
        name="Outlier",
        instrument_type=InstrumentType.stock,
        category="SHARES",
    )
    service = QualityFactsService(
        FundamentalsStub(snapshot),  # type: ignore[arg-type]
        InstrumentsStub({"OUT3": instrument_data("OUT3", instrument)}),  # type: ignore[arg-type]
        OpportunityStub({"OUT3": opportunity("OUT3", instrument)}),  # type: ignore[arg-type]
    )

    response = await service.resolve(
        QualityFactsRequest(
            assets=[QualityAssetRequest(ticker="OUT3", kind=QualityAssetKind.stock)]
        )
    )

    result = response.assets[0]
    facts = {fact.key: fact for fact in result.facts}
    assert facts["revenue_cagr"].status == "missing_data"
    assert facts["earnings_cagr"].status == "missing_data"
    assert facts["share_dilution"].status == "missing_data"
    assert len(result.warnings) == 3


async def test_share_dilution_uses_only_the_latest_five_annual_periods() -> None:
    periods = [
        annual_period(2019, "700", "10"),
        annual_period(2020, "750", "20"),
        annual_period(2021, "800", "100"),
        annual_period(2022, "850", "100"),
        annual_period(2023, "900", "99"),
        annual_period(2024, "950", "98"),
        annual_period(2025, "1000", "97"),
    ]
    snapshot = FundamentalsSnapshot(
        ticker="SPLT3",
        sector="Bens Industriais",
        periods=periods,
        trailing_twelve_months=periods[-1],
    )
    instrument = InstrumentMetadata(
        ticker="SPLT3",
        name="Split History",
        instrument_type=InstrumentType.stock,
        category="SHARES",
    )
    service = QualityFactsService(
        FundamentalsStub(snapshot),  # type: ignore[arg-type]
        InstrumentsStub({"SPLT3": instrument_data("SPLT3", instrument)}),  # type: ignore[arg-type]
        OpportunityStub({"SPLT3": opportunity("SPLT3", instrument)}),  # type: ignore[arg-type]
    )

    result = (
        await service.resolve(
            QualityFactsRequest(
                assets=[QualityAssetRequest(ticker="SPLT3", kind=QualityAssetKind.stock)]
            )
        )
    ).assets[0]
    dilution = next(fact for fact in result.facts if fact.key == "share_dilution")

    assert dilution.status == "valid"
    assert dilution.value is not None
    assert dilution.value < 0


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
    # A foreign listing is derived from the same statements as a Brazilian one,
    # so it carries the full accounting evidence rather than a thin parallel set.
    assert {"gross_margin", "net_margin", "return_on_equity"} <= {fact.key for fact in result.facts}


async def test_international_facts_declare_their_own_source() -> None:
    """The shared derivation labels its facts as CVM; the origin must be restated."""
    instrument = InstrumentMetadata(
        ticker="ACME",
        instrument_type=InstrumentType.stock,
        category="INTERNATIONAL",
        isin="US0000000001",
    )
    snapshot = stock_snapshot()
    for period in snapshot.periods:
        period.source = "investidor10"
    service = QualityFactsService(
        FundamentalsStub(snapshot),  # type: ignore[arg-type]
        InstrumentsStub({"ACME": instrument_data("ACME", instrument)}),  # type: ignore[arg-type]
        OpportunityStub({"ACME": opportunity("ACME", instrument)}),  # type: ignore[arg-type]
    )

    response = await service.resolve(
        QualityFactsRequest(
            assets=[QualityAssetRequest(ticker="ACME", kind=QualityAssetKind.stock)]
        )
    )

    result = response.assets[0]
    assert result.sources == ["investidor10"]
    assert not any(fact.source == "cvm" for fact in result.facts)


async def test_international_facts_report_their_missing_evidence() -> None:
    instrument = InstrumentMetadata(
        ticker="ACME",
        instrument_type=InstrumentType.stock,
        category="INTERNATIONAL",
    )
    service = QualityFactsService(
        FundamentalsStub(stock_snapshot()),  # type: ignore[arg-type]
        InstrumentsStub({"ACME": instrument_data("ACME", instrument)}),  # type: ignore[arg-type]
        OpportunityStub({"ACME": opportunity("ACME", instrument)}),  # type: ignore[arg-type]
    )

    response = await service.resolve(
        QualityFactsRequest(
            assets=[QualityAssetRequest(ticker="ACME", kind=QualityAssetKind.stock)]
        )
    )

    assert any("peers" in warning for warning in response.assets[0].warnings)


async def test_reports_missing_international_fundamentals() -> None:
    """Without statements the thin public profile is the only evidence left."""
    instrument = InstrumentMetadata(
        ticker="EMPTY",
        instrument_type=InstrumentType.stock,
        category="INTERNATIONAL",
    )
    service = QualityFactsService(
        FundamentalsStub(FundamentalsSnapshot(ticker="EMPTY")),  # type: ignore[arg-type]
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
    assert response.assets[0].profile == "broad"
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


@pytest.mark.parametrize(
    ("description", "sectors", "expected_profile"),
    [
        ("Bitcoin exchange traded fund", [], "crypto"),
        ("Global aggregate bond fund", [], "fixed_income"),
        (
            "Sector equity fund",
            [FundAllocation(name="Technology", weight=Decimal("0.70"))],
            "thematic",
        ),
    ],
)
async def test_classifies_etf_methodology_from_public_profile(
    description: str,
    sectors: list[FundAllocation],
    expected_profile: str,
) -> None:
    instrument = InstrumentMetadata(ticker="ETF", instrument_type=InstrumentType.etf)
    profile = FundProfile(
        description=description,
        net_assets=Decimal("100000000"),
        inception_date=date(2020, 1, 1),
        holdings=[FundHolding(symbol="A", weight=Decimal("1"))],
        sectors=sectors,
        source="public_fund_profile",
    )
    service = QualityFactsService(
        FundamentalsStub(stock_snapshot()),  # type: ignore[arg-type]
        InstrumentsStub({"ETF": instrument_data("ETF", instrument, fund_profile=profile)}),  # type: ignore[arg-type]
        OpportunityStub({"ETF": opportunity("ETF", instrument)}),  # type: ignore[arg-type]
    )

    result = (
        await service.resolve(
            QualityFactsRequest(
                assets=[QualityAssetRequest(ticker="ETF", kind=QualityAssetKind.etf)]
            )
        )
    ).assets[0]

    assert result.profile == expected_profile


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
                    reports=list(reversed(reports)),
                    distributions=list(reversed(distributions)),
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


async def test_fi_infra_derives_monthly_nav_returns_from_daily_cvm_history() -> None:
    instrument = InstrumentMetadata(
        ticker="JURO11",
        instrument_type=InstrumentType.fi_infra,
    )
    reports = [
        FundMonthlyReport(
            as_of=date(2025 + index // 12, index % 12 + 1, 28),
            nav_per_share=Decimal("100") + Decimal(index),
        )
        for index in range(18)
    ]
    distributions = [
        FundDistribution(
            ex_date=date(2025 + index // 12, index % 12 + 1, 15),
            value=Decimal("1"),
            source="public",
        )
        for index in range(18)
    ]
    service = QualityFactsService(
        FundamentalsStub(stock_snapshot()),  # type: ignore[arg-type]
        InstrumentsStub({"JURO11": instrument_data("JURO11", instrument)}),  # type: ignore[arg-type]
        OpportunityStub(
            {
                "JURO11": opportunity(
                    "JURO11",
                    instrument,
                    reports=reports,
                    distributions=distributions,
                )
            }
        ),  # type: ignore[arg-type]
    )

    result = (
        await service.resolve(
            QualityFactsRequest(
                assets=[
                    QualityAssetRequest(
                        ticker="JURO11",
                        kind=QualityAssetKind.real_estate_fund,
                    )
                ]
            )
        )
    ).assets[0]
    facts = {fact.key: fact for fact in result.facts}

    assert result.profile == "fi_infra"
    assert facts["nav_return_volatility"].value is not None
    assert facts["positive_nav_return_frequency"].value == Decimal("1")


async def test_fund_facts_measure_nav_preservation_reporting_and_distribution_cuts() -> None:
    instrument = InstrumentMetadata(ticker="FUND11", instrument_type=InstrumentType.fii)
    reports = [
        FundMonthlyReport(
            as_of=date(2024 + index // 12, index % 12 + 1, 1),
            nav_per_share=Decimal("100") + Decimal(index),
            monthly_distribution_yield=Decimal("0.001"),
            monthly_nav_return=Decimal("0.005"),
            net_assets=(Decimal("100") + Decimal(index))
            * (Decimal("1100000") if index >= 12 else Decimal("1000000")),
            issued_shares=Decimal("1100000") if index >= 12 else Decimal("1000000"),
            shareholder_count=Decimal("10000") + Decimal(index * 100),
            administration_fee_ratio=Decimal("0.008"),
            total_assets=(Decimal("100") + Decimal(index))
            * (Decimal("1100000") if index >= 12 else Decimal("1000000"))
            + Decimal("10000000"),
            total_liabilities=Decimal("10000000"),
            property_assets=(Decimal("95") + Decimal(index)) * Decimal("1000000"),
            credit_assets=Decimal("0"),
            liquid_assets=Decimal("10000000"),
            inception_date=date(2016, 1, 1),
            administrator="TRUSTED ADMIN",
        )
        for index in range(24)
    ]
    distributions = [
        FundDistribution(
            ex_date=date(2024 + index // 12, index % 12 + 1, 15),
            value=Decimal("1") if index != 18 else Decimal("0.70"),
            source="cvm",
        )
        for index in range(24)
    ]
    service = QualityFactsService(
        FundamentalsStub(stock_snapshot()),  # type: ignore[arg-type]
        InstrumentsStub({"FUND11": instrument_data("FUND11", instrument)}),  # type: ignore[arg-type]
        OpportunityStub(
            {
                "FUND11": opportunity(
                    "FUND11",
                    instrument,
                    reports=list(reversed(reports)),
                    distributions=list(reversed(distributions)),
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
                    profile="indeterminado",
                )
            ]
        )
    )

    facts = {fact.key: fact for fact in response.assets[0].facts}
    assert response.assets[0].profile == "brick"
    assert facts["reporting_regularity"].value == Decimal("1")
    assert facts["report_completeness"].value == Decimal("1")
    assert facts["nav_growth"].value is not None
    assert facts["nav_return_volatility"].value == Decimal("0")
    assert facts["positive_nav_return_frequency"].value == Decimal("1")
    assert facts["nav_max_drawdown"].value == Decimal("0")
    assert facts["distribution_growth"].value is not None
    assert facts["distribution_cut_frequency"].value is not None
    assert facts["net_assets"].value == Decimal("135300000")
    assert facts["shareholder_count"].value == Decimal("12300")
    assert facts["administration_fee_ratio"].value == Decimal("0.008")
    assert facts["administrator_stability"].value == Decimal("1")
    assert facts["daily_traded_value"].value == Decimal("50000000")
    assert facts["property_allocation"].value is not None
    assert facts["issuance_nav_preservation"].value == Decimal("1")
    assert facts["nav_total_consistency_error"].value == Decimal("0")
    consistency = facts["distribution_report_consistency_error"].value
    assert consistency is not None
    assert consistency > Decimal("0.25")
    assert response.assets[0].warnings == [
        "Distribution values diverge from the corresponding CVM monthly reports"
    ]


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
