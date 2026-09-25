from __future__ import annotations

import asyncio
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
    FundCreditHolding,
    FundCreditPortfolio,
    FundDistribution,
    FundHolding,
    FundMonthlyDistribution,
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
from app.models.assets import FundDistributionEvidence, OpportunityObservation
from app.models.quality import (
    QualityAssetKind,
    QualityAssetRequest,
    QualityFactsRequest,
    QualityFactsResponse,
)
from app.services.bcb_quality import BankQualitySnapshot, MacroQualitySnapshot
from app.services.quality import (
    QualityFactsService,
    _fund_facts,
    _has_domestic_stock_instrument,
    _market_scale_facts,
    _metric_fact,
)

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
        self.calls: list[tuple[str, InstrumentType | None]] = []

    async def get(
        self,
        ticker: str,
        _instrument_type: InstrumentType | None = None,
    ) -> InstrumentDataResponse:
        self.calls.append((ticker, _instrument_type))
        return self.values[ticker]


class OpportunityStub:
    def __init__(self, values: dict[str, OpportunityResponse]) -> None:
        self.values = values
        self.calls: list[str] = []

    async def opportunity(self, ticker: str) -> OpportunityResponse:
        self.calls.append(ticker)
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
    monthly_distributions: list[FundMonthlyDistribution] | None = None,
    distribution_evidence: list[FundDistributionEvidence] | None = None,
    credit_portfolio: FundCreditPortfolio | None = None,
    cnpj: str = "123",
) -> OpportunityResponse:
    return OpportunityResponse(
        ticker=ticker,
        instrument=instrument,
        metrics=opportunity_metrics(),
        fund_reports=FundReportSeries(cnpj=cnpj, reports=reports or []),
        fund_distributions=distributions or [],
        fund_monthly_distributions=monthly_distributions or [],
        fund_distribution_evidence=distribution_evidence or [],
        fund_credit_portfolio=credit_portfolio,
        refreshed_at=NOW,
    )


def distribution_evidence(
    event_dates: list[date],
    unresolved_indices: set[int],
) -> list[FundDistributionEvidence]:
    return [
        FundDistributionEvidence(
            ex_date=event_date,
            value=Decimal("1") if index not in unresolved_indices else None,
            status="single_source" if index not in unresolved_indices else "conflict",
            reason=(
                "Only one independent source was available"
                if index not in unresolved_indices
                else "Independent sources disagree without a clear majority"
            ),
            confidence=Decimal("0.55") if index not in unresolved_indices else Decimal("0"),
            sources=["cvm"]
            if index not in unresolved_indices
            else ["fundamentus", "status_invest"],
            independent_sources=["cvm"]
            if index not in unresolved_indices
            else ["fundamentus", "status_invest"],
            source_lineage=["cvm"]
            if index not in unresolved_indices
            else ["fundamentus", "status_invest"],
            observations=(
                []
                if index not in unresolved_indices
                else [
                    OpportunityObservation(
                        value=Decimal("1"),
                        source="fundamentus",
                        as_of=event_date,
                        unit="BRL",
                        source_lineage=["fundamentus"],
                        independent_origin="fundamentus",
                    ),
                    OpportunityObservation(
                        value=Decimal("1.5"),
                        source="status_invest",
                        as_of=event_date,
                        unit="BRL",
                        source_lineage=["status_invest"],
                        independent_origin="status_invest",
                    ),
                ]
            ),
        )
        for index, event_date in enumerate(event_dates)
    ]


def fund_quality_service(
    ticker: str,
    instrument: InstrumentMetadata,
    reports: list[FundMonthlyReport],
    distributions: list[FundDistribution],
    evidence: list[FundDistributionEvidence],
) -> QualityFactsService:
    return QualityFactsService(
        FundamentalsStub(stock_snapshot()),  # type: ignore[arg-type]
        InstrumentsStub({ticker: instrument_data(ticker, instrument)}),  # type: ignore[arg-type]
        OpportunityStub(
            {
                ticker: opportunity(
                    ticker,
                    instrument,
                    reports=reports,
                    distributions=distributions,
                    distribution_evidence=evidence,
                )
            }
        ),  # type: ignore[arg-type]
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
    assert response.assets[0].error_code == "INVALID_TICKER"
    assert response.assets[0].retryable is False
    assert "network-data" in (response.assets[1].unavailable_reason or "")


async def test_quality_awaits_instrument_cleanup_when_opportunity_fails() -> None:
    class InstrumentProbe:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()
            self.cleaned = asyncio.Event()
            self.finished = asyncio.Event()

        async def get(
            self,
            _ticker: str,
            _instrument_type: InstrumentType | None = None,
        ) -> InstrumentDataResponse:
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                await asyncio.sleep(0)
                self.cleaned.set()
                raise
            finally:
                self.finished.set()
            raise AssertionError("unreachable")

    class FailingOpportunity:
        async def opportunity(self, _ticker: str) -> OpportunityResponse:
            await instrument.started.wait()
            raise RuntimeError("opportunity provider failed")

    instrument = InstrumentProbe()
    service = QualityFactsService(
        FundamentalsStub(stock_snapshot()),  # type: ignore[arg-type]
        instrument,  # type: ignore[arg-type]
        FailingOpportunity(),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="opportunity provider failed"):
        await service.resolve(
            QualityFactsRequest(
                assets=[QualityAssetRequest(ticker="TEST3", kind=QualityAssetKind.stock)]
            )
        )

    assert instrument.cancelled.is_set()
    assert instrument.cleaned.is_set()
    assert instrument.finished.is_set()


async def test_quality_cancels_shared_provider_tasks_when_batch_is_cancelled() -> None:
    class BlockingSectorProvider(FundamentalsStub):
        def __init__(self) -> None:
            super().__init__(stock_snapshot())
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()
            self.cleaned = asyncio.Event()
            self.finished = asyncio.Event()
            self.task: asyncio.Task[object] | None = None

        async def sector_universe(self) -> dict[str, list[SectorCompany]]:
            self.task = asyncio.current_task()
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                await asyncio.sleep(0)
                self.cleaned.set()
                raise
            finally:
                self.finished.set()
            raise AssertionError("unreachable")

    class BlockingMacroProvider:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()
            self.cleaned = asyncio.Event()
            self.finished = asyncio.Event()
            self.task: asyncio.Task[object] | None = None

        async def snapshot(self) -> MacroQualitySnapshot:
            self.task = asyncio.current_task()
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                await asyncio.sleep(0)
                self.cleaned.set()
                raise
            finally:
                self.finished.set()
            raise AssertionError("unreachable")

    sector = BlockingSectorProvider()
    macro = BlockingMacroProvider()
    instrument = InstrumentMetadata(
        ticker="TEST3",
        name="Test",
        instrument_type=InstrumentType.stock,
        category="SHARES",
        country="BR",
        exchange="B3",
        source="b3",
    )
    service = QualityFactsService(
        sector,  # type: ignore[arg-type]
        InstrumentsStub({}),  # type: ignore[arg-type]
        OpportunityStub({}),  # type: ignore[arg-type]
        macro_provider=macro,  # type: ignore[arg-type]
    )
    request = QualityFactsRequest(
        assets=[QualityAssetRequest(ticker="TEST3", kind=QualityAssetKind.stock)]
    )
    resolve_task = asyncio.create_task(
        service.resolve(
            request,
            opportunity_by_ticker={"TEST3": opportunity("TEST3", instrument)},
            fundamentals_by_ticker={"TEST3": stock_snapshot()},
        )
    )

    await asyncio.wait_for(
        asyncio.gather(sector.started.wait(), macro.started.wait()),
        timeout=1,
    )
    resolve_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await resolve_task

    for provider in (sector, macro):
        assert provider.cancelled.is_set()
        assert provider.cleaned.is_set()
        assert provider.finished.is_set()
        assert provider.task is not None and provider.task.done() and provider.task.cancelled()


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


async def test_quality_reuses_provided_domestic_stock_identity_without_lookup() -> None:
    instrument = InstrumentMetadata(
        ticker="TEST3",
        name="Test",
        instrument_type=InstrumentType.stock,
        category="SHARES",
        isin="BRTESTACNOR0",
        country="BR",
        exchange="B3",
    )
    instruments = InstrumentsStub({})
    service = QualityFactsService(
        FundamentalsStub(stock_snapshot()),  # type: ignore[arg-type]
        instruments,  # type: ignore[arg-type]
        OpportunityStub({}),  # type: ignore[arg-type]
    )

    response = await service.resolve(
        QualityFactsRequest(
            assets=[QualityAssetRequest(ticker="TEST3", kind=QualityAssetKind.stock)]
        ),
        opportunity_by_ticker={"TEST3": opportunity("TEST3", instrument)},
        fundamentals_by_ticker={"TEST3": stock_snapshot()},
    )

    assert response.assets[0].facts
    assert instruments.calls == []


def test_domestic_stock_identity_reuse_requires_authoritative_metadata() -> None:
    domestic = InstrumentMetadata(
        ticker="TEST3",
        instrument_type=InstrumentType.stock,
        category="SHARES",
        country="BR",
        exchange="B3",
    )
    assert _has_domestic_stock_instrument(opportunity("TEST3", domestic))
    assert not _has_domestic_stock_instrument(None)
    assert not _has_domestic_stock_instrument(
        opportunity("TEST3", domestic.model_copy(update={"source": "status_invest"}))
    )
    assert not _has_domestic_stock_instrument(
        opportunity(
            "TEST3",
            domestic.model_copy(update={"instrument_type": InstrumentType.bdr}),
        )
    )
    assert not _has_domestic_stock_instrument(
        opportunity("TEST3", domestic.model_copy(update={"category": "INTERNATIONAL"}))
    )
    assert not _has_domestic_stock_instrument(
        opportunity("TEST3", domestic.model_copy(update={"country": "US"}))
    )
    assert not _has_domestic_stock_instrument(
        opportunity("TEST3", domestic.model_copy(update={"exchange": "NYSE"}))
    )


@pytest.mark.parametrize(
    ("ticker", "instrument_type", "category"),
    [
        ("ACME", InstrumentType.stock, "INTERNATIONAL"),
        ("AAPL34", InstrumentType.bdr, "SHARES"),
    ],
)
async def test_quality_keeps_instrument_lookup_for_non_domestic_opportunity_identity(
    ticker: str,
    instrument_type: InstrumentType,
    category: str,
) -> None:
    instrument = InstrumentMetadata(
        ticker=ticker,
        name="Test",
        instrument_type=instrument_type,
        category=category,
        isin="US0000000001" if category == "INTERNATIONAL" else "BRAAPL34E1",
    )
    instruments = InstrumentsStub({ticker: instrument_data(ticker, instrument)})
    service = QualityFactsService(
        FundamentalsStub(stock_snapshot()),  # type: ignore[arg-type]
        instruments,  # type: ignore[arg-type]
        OpportunityStub({}),  # type: ignore[arg-type]
    )
    supplied_snapshot = stock_snapshot().model_copy(update={"ticker": ticker})

    response = await service.resolve(
        QualityFactsRequest(
            assets=[QualityAssetRequest(ticker=ticker, kind=QualityAssetKind.stock)]
        ),
        opportunity_by_ticker={ticker: opportunity(ticker, instrument)},
        fundamentals_by_ticker={ticker: supplied_snapshot},
    )

    assert response.assets[0].facts
    assert instruments.calls == [(ticker, InstrumentType.stock)]


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
        period.source = "public_filings"
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
    assert result.sources == ["public_filings"]
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


async def test_domestic_crypto_etf_preserves_official_cost_and_assets_dates() -> None:
    instrument = InstrumentMetadata(
        ticker="ABTC11",
        instrument_type=InstrumentType.etf,
        isin="BRABTCCTF002",
    )
    source = "https://www.btgpactual.com/asset-management/etf/ABTC11"
    profile = FundProfile(
        net_assets=Decimal("23141900.56"),
        net_assets_date=date(2026, 9, 23),
        net_assets_source=source,
        net_expense_ratio=Decimal("0.0039"),
        inception_date=date(2026, 7, 14),
        description="TEVA BITCOIN FEAR ARBITRAGE",
        source=source,
    )
    data = instrument_data("ABTC11", instrument, fund_profile=profile).model_copy(
        update={"refreshed_at": datetime(2026, 9, 24, tzinfo=UTC)}
    )
    service = QualityFactsService(
        FundamentalsStub(stock_snapshot()),  # type: ignore[arg-type]
        InstrumentsStub({"ABTC11": data}),  # type: ignore[arg-type]
        OpportunityStub({}),  # type: ignore[arg-type]
    )

    asset = (
        await service.resolve(
            QualityFactsRequest(
                assets=[QualityAssetRequest(ticker="ABTC11", kind=QualityAssetKind.etf)]
            )
        )
    ).assets[0]
    facts = {fact.key: fact for fact in asset.facts}

    assert asset.profile == "crypto"
    assert asset.canonical_id == "BRABTCCTF002"
    assert asset.sources == [source]
    assert facts["expense_ratio"].value == Decimal("0.0039")
    assert facts["expense_ratio"].as_of == date(2026, 9, 24)
    assert facts["net_assets"].value == Decimal("23141900.56")
    assert facts["net_assets"].as_of == date(2026, 9, 23)
    assert facts["net_assets"].source == source
    assert facts["holdings_count"].value is None


async def test_assessment_can_reuse_opportunity_without_a_second_provider_call() -> None:
    instrument = InstrumentMetadata(ticker="TEST3", instrument_type=InstrumentType.stock)
    opportunity_provider = OpportunityStub({"TEST3": opportunity("TEST3", instrument)})
    service = QualityFactsService(
        FundamentalsStub(stock_snapshot()),  # type: ignore[arg-type]
        InstrumentsStub({"TEST3": instrument_data("TEST3", instrument)}),  # type: ignore[arg-type]
        opportunity_provider,  # type: ignore[arg-type]
    )

    await service.resolve(
        QualityFactsRequest(
            assets=[QualityAssetRequest(ticker="TEST3", kind=QualityAssetKind.stock)]
        ),
        opportunity_by_ticker={"TEST3": opportunity("TEST3", instrument)},
    )

    assert opportunity_provider.calls == []


async def test_quality_does_not_repeat_a_failed_b3_identity_lookup() -> None:
    instrument = InstrumentMetadata(ticker="TEST3", instrument_type=InstrumentType.stock)
    provided = opportunity("TEST3", instrument).model_copy(
        update={
            "instrument": None,
            "source_failures": {"b3": "PROVIDER_UNAVAILABLE"},
        }
    )
    instruments = InstrumentsStub({})
    service = QualityFactsService(
        FundamentalsStub(stock_snapshot()),  # type: ignore[arg-type]
        instruments,  # type: ignore[arg-type]
        OpportunityStub({}),  # type: ignore[arg-type]
    )

    result = await service.resolve(
        QualityFactsRequest(
            assets=[QualityAssetRequest(ticker="TEST3", kind=QualityAssetKind.stock)]
        ),
        opportunity_by_ticker={"TEST3": provided},
        fundamentals_by_ticker={"TEST3": stock_snapshot()},
    )

    assert instruments.calls == []
    assert result.assets[0].unavailable_reason == "Instrument metadata unavailable"


async def test_etf_quality_does_not_call_equity_opportunity_provider() -> None:
    instrument = InstrumentMetadata(ticker="ETF", instrument_type=InstrumentType.etf)
    profile = FundProfile(
        net_assets=Decimal("100000000"),
        inception_date=date(2020, 1, 1),
        holdings=[FundHolding(symbol="A", weight=Decimal("1"))],
        source="public_fund_profile",
    )
    opportunity_provider = OpportunityStub({})
    service = QualityFactsService(
        FundamentalsStub(stock_snapshot()),  # type: ignore[arg-type]
        InstrumentsStub({"ETF": instrument_data("ETF", instrument, fund_profile=profile)}),  # type: ignore[arg-type]
        opportunity_provider,  # type: ignore[arg-type]
    )

    response = await service.resolve(
        QualityFactsRequest(assets=[QualityAssetRequest(ticker="ETF", kind=QualityAssetKind.etf)])
    )

    assert response.assets[0].unavailable_reason is None
    assert opportunity_provider.calls == []


@pytest.mark.parametrize("use_provided_opportunity", [False, True])
async def test_real_estate_fund_quality_skips_instrument_lookup(
    use_provided_opportunity: bool,
) -> None:
    instrument = InstrumentMetadata(ticker="FUND11", instrument_type=InstrumentType.fii)
    resolved_opportunity = opportunity("FUND11", instrument)
    instruments = InstrumentsStub({})
    opportunity_provider = OpportunityStub({"FUND11": resolved_opportunity})
    service = QualityFactsService(
        FundamentalsStub(stock_snapshot()),  # type: ignore[arg-type]
        instruments,  # type: ignore[arg-type]
        opportunity_provider,  # type: ignore[arg-type]
    )
    request = QualityFactsRequest(
        assets=[
            QualityAssetRequest(
                ticker="FUND11",
                kind=QualityAssetKind.real_estate_fund,
            )
        ]
    )
    if use_provided_opportunity:
        response = await service.resolve(
            request,
            opportunity_by_ticker={"FUND11": resolved_opportunity},
        )
    else:
        response = await service.resolve(request)

    assert response.assets[0].facts
    assert instruments.calls == []
    assert opportunity_provider.calls == ([] if use_provided_opportunity else ["FUND11"])


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


def test_manager_monthly_history_counts_zero_without_creating_cash_event() -> None:
    months = [(2025 + (8 + index) // 12, (8 + index) % 12 + 1) for index in range(12)]
    values = [
        Decimal(value)
        for value in ("1", "1", "1", "1", "1", "1", "1", ".75", ".5", "0", ".75", "1")
    ]
    monthly = [
        FundMonthlyDistribution(
            reference_month=date(year, month, 1),
            payment_date=date(year + (month == 12), month % 12 + 1, 15),
            value=value,
            report_as_of=date(2026, 8, 31),
            source="sparta_manager",
        )
        for (year, month), value in zip(months, values, strict=True)
    ]
    events = [
        FundDistribution(ex_date=row.reference_month, value=row.value, source="b3")
        for row in monthly
        if row.value > 0
    ]
    instrument = InstrumentMetadata(
        ticker="JURO11", instrument_type=InstrumentType.fi_infra, isin="BRJUROCTF002"
    )
    result = _fund_facts(
        QualityAssetRequest(ticker="JURO11", kind=QualityAssetKind.real_estate_fund),
        opportunity(
            "JURO11",
            instrument,
            distributions=events,
            monthly_distributions=monthly,
            cnpj="42730834000100",
        ),
    )
    facts = {fact.key: fact.value for fact in result.facts}

    assert len(events) == 11
    assert facts["distribution_history_months"] == Decimal("12")
    assert facts["positive_distribution_frequency"] == Decimal(11) / Decimal(12)
    assert facts["distribution_growth"] == Decimal("-1") / Decimal(3)
    assert facts["distribution_cut_frequency"] == Decimal("0.3")

    conflicting_aggregator = FundDistributionEvidence(
        ex_date=date(2026, 7, 15),
        status="conflict",
        reason="Aggregators disagree",
        sources=["fundamentus", "status_invest"],
    )
    with_conflict = _fund_facts(
        QualityAssetRequest(ticker="JURO11", kind=QualityAssetKind.real_estate_fund),
        opportunity(
            "JURO11",
            instrument,
            distributions=events,
            monthly_distributions=monthly,
            distribution_evidence=[conflicting_aggregator],
            cnpj="42730834000100",
        ),
    )
    facts_with_conflict = {fact.key: fact.value for fact in with_conflict.facts}
    assert facts_with_conflict["distribution_history_months"] == Decimal("12")
    assert facts_with_conflict["positive_distribution_frequency"] == Decimal(11) / Decimal(12)
    assert not any("withheld" in warning for warning in with_conflict.warnings)


def test_juro_credit_inventory_exposes_issue_weights_without_implied_default_risk() -> None:
    instrument = InstrumentMetadata(
        ticker="JURO11", instrument_type=InstrumentType.fi_infra, isin="BRJUROCTF002"
    )
    portfolio = FundCreditPortfolio(
        report_as_of=date(2026, 8, 31),
        source="sparta_manager",
        document_digest="a" * 64,
        document_url="https://sparta.com.br/uploads/JURO11_RelatorioMensal_2026_08.pdf",
        holdings=[
            FundCreditHolding(
                row_number=number,
                security_code=f"ISSUE{number}",
                issuer_and_sector="Issuer Rodovias",
                disclosed_rating=rating,
                credit_spread=Decimal("0.009"),
                duration_years=Decimal("6.6"),
                portfolio_weight=Decimal(weight),
            )
            for number, rating, weight in ((1, "AAA", "0.03"), (2, "S/R", "0.02"))
        ],
        cash_weight=Decimal("0.95"),
    )
    result = _fund_facts(
        QualityAssetRequest(ticker="JURO11", kind=QualityAssetKind.real_estate_fund),
        opportunity("JURO11", instrument, credit_portfolio=portfolio),
    )
    facts = {fact.key: fact for fact in result.facts}

    assert facts["credit_issue_count"].value == Decimal("2")
    assert facts["credit_reported_weight"].value == Decimal("0.05")
    assert facts["credit_unrated_weight"].value == Decimal("0.02")
    assert facts["credit_largest_issue_weight"].value == Decimal("0.03")
    assert facts["credit_cash_weight"].value == Decimal("0.95")
    assert all(
        facts[key].source_lineage == ["sparta_manager"] and facts[key].independent_source_count == 1
        for key in (
            "credit_issue_count",
            "credit_reported_weight",
            "credit_unrated_weight",
            "credit_largest_issue_weight",
            "credit_cash_weight",
        )
    )
    assert "sparta_manager" in result.sources
    assert result.unavailable_reason is None


async def test_fund_quality_facts_use_confirmed_quota_basis() -> None:
    instrument = InstrumentMetadata(ticker="ALZR11", instrument_type=InstrumentType.fii)
    months = [(2024 + (month + 7) // 12, (month + 7) % 12 + 1) for month in range(18)]
    reports = [
        FundMonthlyReport(
            as_of=date(year, month, 1),
            nav_per_share=Decimal("100") if index < 8 else Decimal("10"),
            issued_shares=Decimal("1000000") if index < 8 else Decimal("10000000"),
        )
        for index, (year, month) in enumerate(months)
    ]
    distributions = [
        FundDistribution(
            ex_date=date(year, month, 15),
            value=Decimal("1") if index < 9 else Decimal("0.1"),
            source="cvm",
        )
        for index, (year, month) in enumerate(months)
    ]
    service = QualityFactsService(
        FundamentalsStub(stock_snapshot()),  # type: ignore[arg-type]
        InstrumentsStub({"ALZR11": instrument_data("ALZR11", instrument)}),  # type: ignore[arg-type]
        OpportunityStub(
            {
                "ALZR11": opportunity(
                    "ALZR11",
                    instrument,
                    reports=reports,
                    distributions=distributions,
                    cnpj="28.737.771/0001-85",
                )
            }
        ),  # type: ignore[arg-type]
    )

    asset = (
        await service.resolve(
            QualityFactsRequest(
                assets=[
                    QualityAssetRequest(ticker="ALZR11", kind=QualityAssetKind.real_estate_fund)
                ]
            )
        )
    ).assets[0]

    facts = {fact.key: fact for fact in asset.facts}
    assert facts["nav_growth"].value == Decimal("0")
    assert facts["nav_max_drawdown"].value == Decimal("0")
    assert facts["distribution_growth"].value == Decimal("0")
    assert facts["distribution_cut_frequency"].value == Decimal("0")
    assert asset.warnings[0].startswith("Quota units adjusted")


async def test_unverified_quota_jump_withholds_score_affecting_facts() -> None:
    instrument = InstrumentMetadata(ticker="FUND11", instrument_type=InstrumentType.fii)
    reports = [
        FundMonthlyReport(
            as_of=date(2024 + index // 12, index % 12 + 1, 1),
            nav_per_share=Decimal("100") if index < 12 else Decimal("10"),
            issued_shares=Decimal("1000000") if index < 12 else Decimal("10000000"),
        )
        for index in range(24)
    ]
    distributions = [
        FundDistribution(
            ex_date=date(2024 + index // 12, index % 12 + 1, 15),
            value=Decimal("1") if index < 12 else Decimal("0.1"),
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
                    "FUND11", instrument, reports=reports, distributions=distributions
                )
            }
        ),  # type: ignore[arg-type]
    )

    asset = (
        await service.resolve(
            QualityFactsRequest(
                assets=[
                    QualityAssetRequest(ticker="FUND11", kind=QualityAssetKind.real_estate_fund)
                ]
            )
        )
    ).assets[0]

    facts = {fact.key: fact for fact in asset.facts}
    for key in (
        "nav_growth",
        "nav_max_drawdown",
        "distribution_growth",
        "distribution_cut_frequency",
    ):
        assert facts[key].value is None
    assert "unverified discontinuity" in asset.warnings[0]


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


async def test_fund_distribution_facts_keep_status_invest_provenance() -> None:
    instrument = InstrumentMetadata(ticker="FUND11", instrument_type=InstrumentType.fii)
    event_dates = [date(2026, month, 15) for month in range(1, 7)]
    distributions = [
        FundDistribution(ex_date=event_date, value=Decimal("1"), source="status_invest")
        for event_date in event_dates
    ]
    evidence = [
        FundDistributionEvidence(
            ex_date=event_date,
            value=Decimal("1"),
            status="single_source",
            reason="Only one independent source was available",
            confidence=Decimal("0.55"),
            sources=["status_invest"],
            independent_sources=["status_invest"],
            source_lineage=["status_invest"],
        )
        for event_date in event_dates
    ]
    service = QualityFactsService(
        FundamentalsStub(stock_snapshot()),  # type: ignore[arg-type]
        InstrumentsStub({"FUND11": instrument_data("FUND11", instrument)}),  # type: ignore[arg-type]
        OpportunityStub(
            {
                "FUND11": opportunity(
                    "FUND11",
                    instrument,
                    distributions=distributions,
                    distribution_evidence=evidence,
                )
            }
        ),  # type: ignore[arg-type]
    )

    result = (
        await service.resolve(
            QualityFactsRequest(
                assets=[
                    QualityAssetRequest(
                        ticker="FUND11",
                        kind=QualityAssetKind.real_estate_fund,
                    )
                ]
            )
        )
    ).assets[0]

    facts = {fact.key: fact for fact in result.facts}
    assert "status_invest" in result.sources
    assert "cvm" not in result.sources
    for key in (
        "distribution_history_months",
        "distribution_stability",
        "positive_distribution_frequency",
    ):
        assert facts[key].source == "status_invest"
        assert facts[key].source_lineage == ["status_invest"]
        assert facts[key].independent_source_count == 1
        assert facts[key].confidence == Decimal("0.55")


async def test_fund_with_non_latest_conflict_withholds_truncated_distribution_history() -> None:
    instrument = InstrumentMetadata(ticker="FUND11", instrument_type=InstrumentType.fii)
    event_dates = [date(2025 + index // 12, index % 12 + 1, 15) for index in range(13)]
    reports = [
        FundMonthlyReport(
            as_of=event_date.replace(day=1),
            nav_per_share=Decimal("100"),
            monthly_distribution_yield=Decimal("0.01"),
        )
        for event_date in event_dates
    ]
    distributions = [
        FundDistribution(ex_date=event_date, value=Decimal("1"), source="cvm")
        for index, event_date in enumerate(event_dates)
        if index != 5
    ]
    evidence = [
        FundDistributionEvidence(
            ex_date=event_date,
            value=Decimal("1") if index != 5 else None,
            status="single_source" if index != 5 else "conflict",
            reason=(
                "Only one independent source was available"
                if index != 5
                else "Independent sources disagree without a clear majority"
            ),
            confidence=Decimal("0.55") if index != 5 else Decimal("0"),
            sources=["cvm"] if index != 5 else ["fundamentus", "status_invest"],
            independent_sources=["cvm"] if index != 5 else ["fundamentus", "status_invest"],
            source_lineage=["cvm"] if index != 5 else ["fundamentus", "status_invest"],
            observations=(
                []
                if index != 5
                else [
                    OpportunityObservation(
                        value=Decimal("1"),
                        source="fundamentus",
                        as_of=event_date,
                        unit="BRL",
                        source_lineage=["fundamentus"],
                        independent_origin="fundamentus",
                    ),
                    OpportunityObservation(
                        value=Decimal("1.5"),
                        source="status_invest",
                        as_of=event_date,
                        unit="BRL",
                        source_lineage=["status_invest"],
                        independent_origin="status_invest",
                    ),
                ]
            ),
        )
        for index, event_date in enumerate(event_dates)
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
                    distribution_evidence=evidence,
                )
            }
        ),  # type: ignore[arg-type]
    )

    result = (
        await service.resolve(
            QualityFactsRequest(
                assets=[
                    QualityAssetRequest(
                        ticker="FUND11",
                        kind=QualityAssetKind.real_estate_fund,
                    )
                ]
            )
        )
    ).assets[0]
    facts = {fact.key: fact for fact in result.facts}
    affected = {
        "distribution_history_months",
        "distribution_stability",
        "positive_distribution_frequency",
        "distribution_growth",
        "distribution_cut_frequency",
        "distribution_report_consistency_error",
    }
    for key in affected:
        assert facts[key].value is None
        assert facts[key].status == "missing_data"
        assert facts[key].unavailable_reason == (
            "Recent distribution history contains unresolved source observations"
        )
    assert facts["reporting_history_months"].value == Decimal("13")
    assert facts["reporting_regularity"].value is not None
    assert result.warnings == [
        "Recent distribution history contains unresolved source observations; "
        "distribution-derived history facts were withheld"
    ]


async def test_fund_conflict_outside_recent_distribution_window_keeps_recent_facts() -> None:
    instrument = InstrumentMetadata(ticker="FUND11", instrument_type=InstrumentType.fii)
    event_dates = [date(2023 + index // 12, index % 12 + 1, 15) for index in range(37)]
    reports = [
        FundMonthlyReport(
            as_of=event_date.replace(day=1),
            nav_per_share=Decimal("100"),
            monthly_distribution_yield=Decimal("0.01"),
        )
        for event_date in event_dates
    ]
    distributions = [
        FundDistribution(ex_date=event_date, value=Decimal("1"), source="cvm")
        for index, event_date in enumerate(event_dates)
        if index != 0
    ]
    evidence = [
        FundDistributionEvidence(
            ex_date=event_date,
            value=Decimal("1") if index != 0 else None,
            status="single_source" if index != 0 else "conflict",
            reason=(
                "Only one independent source was available"
                if index != 0
                else "Independent sources disagree without a clear majority"
            ),
            confidence=Decimal("0.55") if index != 0 else Decimal("0"),
            sources=["cvm"] if index != 0 else ["fundamentus", "status_invest"],
            independent_sources=["cvm"] if index != 0 else ["fundamentus", "status_invest"],
            source_lineage=["cvm"] if index != 0 else ["fundamentus", "status_invest"],
        )
        for index, event_date in enumerate(event_dates)
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
                    distribution_evidence=evidence,
                )
            }
        ),  # type: ignore[arg-type]
    )

    result = (
        await service.resolve(
            QualityFactsRequest(
                assets=[
                    QualityAssetRequest(
                        ticker="FUND11",
                        kind=QualityAssetKind.real_estate_fund,
                    )
                ]
            )
        )
    ).assets[0]
    facts = {fact.key: fact for fact in result.facts}
    assert facts["distribution_history_months"].value == Decimal("36")
    assert facts["distribution_stability"].value == Decimal("0")
    assert facts["positive_distribution_frequency"].value == Decimal("1")
    assert facts["distribution_growth"].value is not None
    assert facts["distribution_cut_frequency"].value == Decimal("0")
    assert facts["distribution_report_consistency_error"].value == Decimal("0")
    assert result.warnings == []


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


async def test_quality_batch_isolates_known_provider_schema_failures() -> None:
    class FailingInstruments:
        async def get(
            self,
            _ticker: str,
            _instrument_type: InstrumentType | None = None,
        ) -> InstrumentDataResponse:
            raise ValueError("malformed provider payload")

    service = QualityFactsService(
        FundamentalsStub(stock_snapshot()),  # type: ignore[arg-type]
        FailingInstruments(),  # type: ignore[arg-type]
        OpportunityStub({}),  # type: ignore[arg-type]
    )

    response = await service.resolve(
        QualityFactsRequest(assets=[QualityAssetRequest(ticker="TEST3", kind=QualityAssetKind.etf)])
    )

    assert response.assets[0].unavailable_reason == "Quality evidence unavailable"
    assert response.assets[0].error_code == "QUALITY_RESOLUTION_FAILED"
    assert response.assets[0].retryable is True


async def test_quality_reuses_provided_fundamentals_and_marks_missing_opportunity_evidence() -> (
    None
):
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
        OpportunityStub({}),  # type: ignore[arg-type]
    )

    response = await service.resolve(
        QualityFactsRequest(
            assets=[QualityAssetRequest(ticker="TEST3", kind=QualityAssetKind.stock)]
        ),
        opportunity_by_ticker={"TEST3": None},
        fundamentals_by_ticker={"TEST3": stock_snapshot()},
    )

    facts = {fact.key: fact for fact in response.assets[0].facts}
    assert fundamentals.calls == []
    assert facts["daily_traded_value"].status == "missing_data"
    assert facts["market_capitalization"].status == "missing_data"


async def test_quality_reports_missing_provided_fundamentals_for_domestic_stock() -> None:
    instrument = InstrumentMetadata(
        ticker="TEST3",
        name="Test",
        instrument_type=InstrumentType.stock,
        category="SHARES",
        isin="BRTESTACNOR0",
    )
    service = QualityFactsService(
        FundamentalsStub(stock_snapshot()),  # type: ignore[arg-type]
        InstrumentsStub({"TEST3": instrument_data("TEST3", instrument)}),  # type: ignore[arg-type]
        OpportunityStub({}),  # type: ignore[arg-type]
    )

    response = await service.resolve(
        QualityFactsRequest(
            assets=[QualityAssetRequest(ticker="TEST3", kind=QualityAssetKind.stock)]
        ),
        opportunity_by_ticker={"TEST3": None},
        fundamentals_by_ticker={"TEST3": None},
    )

    assert response.assets[0].unavailable_reason == "Fundamentals evidence unavailable"
    assert response.assets[0].error_code is None
    assert response.assets[0].retryable is False


async def test_quality_reports_missing_or_reuses_provided_international_fundamentals() -> None:
    instrument = InstrumentMetadata(
        ticker="ACME",
        instrument_type=InstrumentType.stock,
        category="INTERNATIONAL",
        isin="US0000000001",
    )
    data = instrument_data("ACME", instrument)
    service = QualityFactsService(
        FundamentalsStub(stock_snapshot()),  # type: ignore[arg-type]
        InstrumentsStub({"ACME": data}),  # type: ignore[arg-type]
        OpportunityStub({}),  # type: ignore[arg-type]
    )
    request = QualityFactsRequest(
        assets=[QualityAssetRequest(ticker="ACME", kind=QualityAssetKind.stock)]
    )

    missing = await service.resolve(
        request,
        opportunity_by_ticker={"ACME": None},
        fundamentals_by_ticker={"ACME": None},
    )
    assert missing.assets[0].unavailable_reason == "Fundamentals evidence unavailable"
    assert missing.assets[0].error_code is None
    assert missing.assets[0].retryable is False

    supplied = stock_snapshot().model_copy(update={"ticker": "ACME"})
    resolved = await service.resolve(
        request,
        opportunity_by_ticker={"ACME": None},
        fundamentals_by_ticker={"ACME": supplied},
    )
    assert resolved.assets[0].facts


def test_quality_market_scale_helpers_explain_absent_opportunity_values() -> None:
    facts = _market_scale_facts(None)

    assert [fact.key for fact in facts] == ["daily_traded_value", "market_capitalization"]
    assert all(fact.status == "missing_data" for fact in facts)
    assert _metric_fact("market_capitalization", None, "currency").unavailable_reason == (
        "Opportunity evidence unavailable"
    )
