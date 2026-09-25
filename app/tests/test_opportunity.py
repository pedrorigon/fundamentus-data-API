import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal, localcontext

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.core.errors import (
    InvalidTickerError,
    ProviderInvalidResponseError,
    ProviderUnavailableError,
)
from app.domain.evidence import ConsensusStatus, SourceObservation, resolve_consensus
from app.models import (
    AssetDetails,
    AssetResponse,
    DetailSection,
    Dividend,
    FieldData,
    FundDistribution,
    FundMonthlyDistribution,
    InstrumentBatchRequest,
    InstrumentMetadata,
    InstrumentType,
    OpportunityMetric,
    OpportunityObservation,
)
from app.models.income_events import CanonicalIncomeEvent, IncomeEventStatus
from app.scrapers.cvm_fund_reports import (
    FundArchiveFailure,
    FundReportPoint,
    FundReportSeries,
)
from app.services.opportunity import (
    B3InstrumentProvider,
    OpportunityService,
    StatusInvestProfile,
    StatusInvestProvider,
    _add_distribution_metrics,
    _add_monthly_distribution_metrics,
    _is_valid_b3_payload,
    _merge_fund_distributions,
    _merge_official_fund_metrics,
    _metric,
    _opportunity_metrics,
    _public_fund_distribution_evidence,
    _public_fund_distributions,
    _reconcile_fund_distributions,
    _verified_fund_cnpj,
    _verified_income_distributions,
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
async def test_opportunity_starts_independent_b3_and_fundamentus_sources_together() -> None:
    b3_started = asyncio.Event()
    fundamentus_started = asyncio.Event()
    release = asyncio.Event()

    class ConcurrentB3Provider(FakeB3Provider):
        async def get(self, ticker: str) -> InstrumentMetadata:
            b3_started.set()
            await release.wait()
            return self._instrument(ticker)

    class ConcurrentAssetService(FakeAssetService):
        async def get_asset(self, ticker: str) -> AssetResponse:
            fundamentus_started.set()
            await release.wait()
            return await super().get_asset(ticker)

    service = OpportunityService(
        ConcurrentAssetService(),  # type: ignore[arg-type]
        Settings(),
        b3_provider=ConcurrentB3Provider(),  # type: ignore[arg-type]
        status_provider=FakeEmptyStatusProvider(),  # type: ignore[arg-type]
        cvm_provider=FakeCvmProvider(),  # type: ignore[arg-type]
    )
    request = asyncio.create_task(service.opportunity("TEST3"))

    await asyncio.wait_for(b3_started.wait(), timeout=1)
    await asyncio.wait_for(fundamentus_started.wait(), timeout=1)
    release.set()

    result = await request
    assert result.instrument is not None
    assert result.metrics.current_price.value == Decimal("30")


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
async def test_opportunity_service_retains_typed_optional_source_failures() -> None:
    class FailedB3Provider(FakeB3Provider):
        async def get(self, ticker: str) -> InstrumentMetadata:
            raise ProviderUnavailableError(ticker=ticker)

    class FailedStatusProvider(FakeStatusProvider):
        async def profile(
            self,
            ticker: str,
            instrument_type: InstrumentType | None,
        ) -> StatusInvestProfile:
            raise ProviderInvalidResponseError(ticker=ticker)

    service = OpportunityService(
        FakeAssetService(),  # type: ignore[arg-type]
        Settings(),
        b3_provider=FailedB3Provider(),  # type: ignore[arg-type]
        status_provider=FailedStatusProvider(),  # type: ignore[arg-type]
    )

    result = await service.opportunity("TEST3")

    assert result.instrument is None
    assert result.source_failures == {
        "b3": "PROVIDER_UNAVAILABLE",
        "status_invest": "PROVIDER_INVALID_RESPONSE",
    }
    assert result.metrics.current_price.value == Decimal("30")


def test_graham_price_uses_decimal_sqrt_without_float_rounding() -> None:
    from app.services.opportunity import _opportunity_metrics

    details = AssetDetails(
        ticker="TEST3",
        quote=Decimal("1"),
        quote_date=date(2026, 7, 10),
        book_value_per_share=Decimal("1E+40"),
        earnings_per_share=Decimal("1E-40"),
        shares_count=Decimal("1"),
        sections=[],
        source_url="https://example.test",
        scraped_at=datetime(2026, 7, 10, tzinfo=UTC),
    )

    metrics = _opportunity_metrics(details, [], {}, Decimal("6"))

    with localcontext() as context:
        context.prec = 34
        expected = Decimal("22.5").sqrt()
    assert metrics.graham_price.value == expected


def test_opportunity_metrics_reject_non_positive_prices() -> None:
    from app.services.opportunity import _opportunity_metrics

    details = AssetDetails(
        ticker="TEST3",
        quote=Decimal("0"),
        quote_date=date(2026, 7, 10),
        book_value_per_share=Decimal("10"),
        earnings_per_share=Decimal("1"),
        shares_count=Decimal("1"),
        sections=[],
        source_url="https://example.test",
        scraped_at=datetime(2026, 7, 10, tzinfo=UTC),
    )

    metrics = _opportunity_metrics(details, [], {"current_price": Decimal("-1")}, Decimal("6"))

    assert metrics.current_price.value is None
    assert metrics.current_price.consensus_status == "invalid_data"
    assert [item.value for item in metrics.current_price.observations] == [
        Decimal("-1"),
        Decimal("0"),
    ]
    assert [item.value for item in metrics.current_price.rejected_observations] == [
        Decimal("-1"),
        Decimal("0"),
    ]


def test_opportunity_models_reject_nonfinite_values_and_invalid_confidence() -> None:
    with pytest.raises(ValueError, match="observation value must be finite"):
        OpportunityObservation.finite_value(Decimal("NaN"))
    with pytest.raises(ValueError, match="metric values must be finite"):
        OpportunityMetric.finite_numbers(Decimal("NaN"))
    with pytest.raises(ValueError, match="between zero and one"):
        OpportunityMetric.confidence_range(Decimal("-0.1"))


@pytest.mark.asyncio
async def test_opportunity_reports_zero_for_a_confirmed_non_dividend_payer() -> None:
    class NoDividendAssetService(FakeAssetService):
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
                                    value=Decimal("0"),
                                    raw_value="0,00%",
                                    value_type="percent",
                                )
                            ],
                        ),
                    ]
                }
            )
            return asset.model_copy(update={"details": details, "dividends": []})

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
    assert len(result.fund_distribution_evidence) == 4
    assert [item.ex_date for item in result.fund_distribution_evidence] == sorted(
        (item.ex_date for item in result.fund_distribution_evidence),
        reverse=True,
    )
    assert all(item.value is not None for item in result.fund_distribution_evidence)


@pytest.mark.parametrize(
    ("status_cnpj", "failure_key", "failure_code"),
    [
        (None, "status_invest", "PROVIDER_UNAVAILABLE"),
        ("43.140.450/0001-92", "status_invest:fund_cnpj", "IDENTITY_CONFLICT"),
    ],
)
async def test_juro11_uses_verified_traded_fund_cnpj(
    status_cnpj: str | None, failure_key: str, failure_code: str
) -> None:
    class JuroB3Provider(FakeB3Provider):
        async def get(self, ticker: str) -> InstrumentMetadata:
            return InstrumentMetadata(
                ticker=ticker,
                name="SPARTA INFRA FIC FI INFRA RENDA FIXA CP",
                instrument_type=InstrumentType.fi_infra,
                isin="BRJUROCTF002",
                source="b3",
                confidence="high",
            )

    class JuroStatusProvider(FakeStatusProvider):
        async def profile(
            self, ticker: str, instrument_type: InstrumentType | None
        ) -> StatusInvestProfile:
            if status_cnpj is None:
                raise ProviderUnavailableError(ticker=ticker)
            return StatusInvestProfile(values={}, cnpj=status_cnpj)

    class CapturedCvmProvider(FakeCvmProvider):
        cnpj_requested: str | None = None

        async def reports(
            self,
            instrument: InstrumentMetadata | None,
            *,
            cnpj: str | None = None,
            today: date | None = None,
        ) -> FundReportSeries:
            self.cnpj_requested = cnpj
            return await super().reports(instrument, cnpj=cnpj, today=today)

    class EmptySpartaProvider:
        async def distributions(self, *, as_of: date | None = None) -> tuple[()]:
            return ()

    cvm = CapturedCvmProvider()
    service = OpportunityService(
        FakeAssetService(),  # type: ignore[arg-type]
        Settings(),
        b3_provider=JuroB3Provider(),  # type: ignore[arg-type]
        status_provider=JuroStatusProvider(),  # type: ignore[arg-type]
        cvm_provider=cvm,  # type: ignore[arg-type]
        sparta_provider=EmptySpartaProvider(),  # type: ignore[arg-type]
    )

    result = await service.opportunity("JURO11")

    assert cvm.cnpj_requested == "42730834000100"
    assert result.fund_reports is not None
    assert result.fund_reports.cnpj == "42730834000100"
    assert result.source_failures[failure_key] == failure_code


def test_verified_b3_fund_income_outweighs_conflicting_aggregators() -> None:
    instrument = InstrumentMetadata(
        ticker="JURO11",
        instrument_type=InstrumentType.fi_infra,
        isin="BRJUROCTF002",
        source="b3",
        confidence="high",
    )
    event = CanonicalIncomeEvent(
        event_id="juro-aug-2026",
        ticker="JURO11",
        isin="BRJUROCTF002",
        event_type="Rendimento",
        ex_date=date(2026, 8, 31),
        payment_date=date(2026, 9, 15),
        unit_price=Decimal("1"),
        status=IncomeEventStatus.verified,
        sources=["b3"],
    )
    authoritative = _verified_income_distributions([event], instrument)
    misleading = Dividend(
        ex_date=event.ex_date,
        payment_date=event.payment_date,
        value=Decimal("9"),
        type="Rendimento",
        is_future_payment=False,
        is_future_ex_date=False,
        raw={},
    )

    reconciled = _reconcile_fund_distributions(
        [misleading],
        (FundDistribution(ex_date=event.ex_date, value=Decimal("8"), source="status_invest"),),
        authoritative,
    )

    assert reconciled[0].consensus.value == Decimal("1")
    assert reconciled[0].consensus.sources == ("b3",)
    assert {item.source for item in reconciled[0].consensus.rejected_observations} == {
        "fundamentus",
        "status_invest",
    }
    assert (
        _verified_income_distributions(
            [event], instrument.model_copy(update={"isin": "BRJUROCTF003"})
        )
        == ()
    )


async def test_manager_months_define_exact_juro_income_metrics() -> None:
    asset = await FakeAssetService().get_asset("JURO11")
    assert asset.details is not None
    metrics = _opportunity_metrics(asset.details, [], {}, Decimal("6"))
    values = ("1", "1", "1", "1", "1", "1", "1", ".75", ".5", "0", ".75", "1")
    monthly = tuple(
        FundMonthlyDistribution(
            reference_month=date(2025 + (8 + index) // 12, (8 + index) % 12 + 1, 1),
            payment_date=date(2026, 9, 15),
            value=Decimal(value),
            report_as_of=date(2026, 8, 31),
            source="sparta_manager",
        )
        for index, value in enumerate(values)
    )

    resolved = _add_monthly_distribution_metrics(metrics, monthly)

    assert resolved.dividends_12m.value == Decimal("10")
    assert resolved.latest_distribution is not None
    assert resolved.latest_distribution.value == Decimal("1")
    assert resolved.median_distribution_3m is not None
    assert resolved.median_distribution_3m.value == Decimal("0.75")
    assert resolved.median_distribution_6m is not None
    assert resolved.median_distribution_6m.value == Decimal("0.75")
    assert resolved.dividend_yield_12m.value == Decimal("100") / Decimal("3")


@pytest.mark.parametrize(
    ("isin", "source", "confidence"),
    [
        ("BRJUROCTF003", "b3", "high"),
        ("BRJUROCTF002", "other", "high"),
        ("BRJUROCTF002", "b3", "low"),
    ],
)
def test_juro11_verified_cnpj_requires_exact_trusted_listing(
    isin: str, source: str, confidence: str
) -> None:
    instrument = InstrumentMetadata(
        ticker="JURO11",
        instrument_type=InstrumentType.fi_infra,
        isin=isin,
        source=source,
        confidence=confidence,
    )

    assert _verified_fund_cnpj(instrument) is None


def _juro_monthly() -> tuple[FundMonthlyDistribution, ...]:
    values = ("1", "1", "1", "1", "1", "1", "1", ".75", ".5", "0", ".75", "1")
    return tuple(
        FundMonthlyDistribution(
            reference_month=date(2025 + (8 + index) // 12, (8 + index) % 12 + 1, 1),
            payment_date=date(2025 + (9 + index) // 12, (9 + index) % 12 + 1, 15),
            value=Decimal(value),
            report_as_of=date(2026, 8, 31),
            source="sparta_manager",
            published_at=datetime(2026, 9, 3, tzinfo=UTC),
        )
        for index, value in enumerate(values)
    )


def _juro_income_event(amount: str = "1") -> CanonicalIncomeEvent:
    return CanonicalIncomeEvent(
        event_id="juro-aug-2026",
        ticker="JURO11",
        isin="BRJUROCTF002",
        event_type="Rendimento",
        ex_date=date(2026, 8, 31),
        payment_date=date(2026, 9, 15),
        unit_price=Decimal(amount),
        status=IncomeEventStatus.verified,
        sources=["b3"],
        updated_at=datetime(2026, 9, 20, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_juro11_service_reconciles_manager_months_with_verified_b3() -> None:
    class JuroB3(FakeB3Provider):
        async def get(self, ticker: str) -> InstrumentMetadata:
            return InstrumentMetadata(
                ticker=ticker,
                instrument_type=InstrumentType.fi_infra,
                isin="BRJUROCTF002",
                source="b3",
                confidence="high",
            )

    class IncomeStore:
        requested: tuple[list[str], date] | None = None
        amount = "1"
        unavailable = False

        async def events(self, tickers: list[str], *, to_date: date) -> list[CanonicalIncomeEvent]:
            if self.unavailable:
                raise RuntimeError("store unavailable")
            self.requested = (tickers, to_date)
            future = _juro_income_event("9").model_copy(
                update={
                    "event_id": "future-revision",
                    "ex_date": date(2026, 7, 31),
                    "payment_date": date(2026, 8, 14),
                    "updated_at": datetime(2026, 9, 26, tzinfo=UTC),
                }
            )
            return [_juro_income_event(self.amount), future]

    class Manager:
        requested: datetime | None = None
        unavailable = False

        async def distributions(
            self, *, as_of: datetime | None = None
        ) -> tuple[FundMonthlyDistribution, ...]:
            if self.unavailable:
                raise ProviderUnavailableError(ticker="JURO11")
            self.requested = as_of
            return _juro_monthly()

    store = IncomeStore()
    manager = Manager()
    service = OpportunityService(
        FakeAssetService(),  # type: ignore[arg-type]
        Settings(),
        b3_provider=JuroB3(),  # type: ignore[arg-type]
        status_provider=FakeEmptyStatusProvider(),  # type: ignore[arg-type]
        cvm_provider=FakeCvmProvider(),  # type: ignore[arg-type]
        income_store=store,  # type: ignore[arg-type]
        sparta_provider=manager,  # type: ignore[arg-type]
    )
    reference = datetime(2026, 9, 25, 12, tzinfo=UTC)

    result = await service.opportunity("JURO11", as_of=reference)

    assert store.requested == (["JURO11"], date(2026, 9, 25))
    assert manager.requested == reference
    assert result.metrics.dividends_12m.value == Decimal("10")
    assert result.metrics.median_distribution_6m is not None
    assert result.metrics.median_distribution_6m.value == Decimal("0.75")
    assert result.fund_monthly_distributions[9].value == Decimal("0")
    assert any(
        item.ex_date == date(2026, 8, 31) and item.value == Decimal("1")
        for item in result.fund_distributions
    )
    assert not any(item.ex_date == date(2026, 7, 31) for item in result.fund_distributions)
    assert result.source_failures == {}

    store.amount = "2"
    conflicting = await service.opportunity("JURO11", as_of=reference)
    assert conflicting.fund_monthly_distributions == []
    assert conflicting.source_failures["sparta_manager"] == "DATA_CONFLICT"

    store.unavailable = True
    manager.unavailable = True
    unavailable = await service.opportunity("JURO11", as_of=reference)
    assert unavailable.source_failures == {
        "income_store": "STORE_UNAVAILABLE",
        "sparta_manager": "PROVIDER_UNAVAILABLE",
    }


def test_manager_history_is_withheld_when_verified_b3_disagrees() -> None:
    from app.services.opportunity import _monthly_income_agrees_with_events

    instrument = InstrumentMetadata(
        ticker="JURO11",
        instrument_type=InstrumentType.fi_infra,
        isin="BRJUROCTF002",
        source="b3",
        confidence="high",
    )

    assert _monthly_income_agrees_with_events(_juro_monthly(), [_juro_income_event()], instrument)
    assert not _monthly_income_agrees_with_events(
        _juro_monthly(), [_juro_income_event("2")], instrument
    )


def test_fund_nav_consensus_drives_price_to_book_and_rejects_cvm_outlier() -> None:
    from app.services.opportunity import _opportunity_metrics

    details = AssetDetails(
        ticker="TEST11",
        quote=Decimal("30"),
        quote_date=date(2026, 7, 10),
        book_value_per_share=Decimal("100"),
        sections=[],
        source_url="https://example.test",
        scraped_at=datetime(2026, 7, 10, tzinfo=UTC),
    )
    metrics = _opportunity_metrics(
        details,
        [],
        {"book_value_per_share": Decimal("100")},
        Decimal("6"),
    )

    merged = _merge_official_fund_metrics(
        metrics,
        FundReportSeries(
            reports=(
                FundReportPoint(
                    as_of=date(2026, 7, 1),
                    nav_per_share=Decimal("10"),
                ),
            )
        ),
    )

    assert merged.book_value_per_share.value == Decimal("100")
    assert merged.book_value_per_share.sources == ["fundamentus", "status_invest"]
    assert merged.book_value_per_share.independent_sources == [
        "fundamentus",
        "status_invest",
    ]
    assert merged.book_value_per_share.as_of == date(2026, 7, 10)
    assert merged.price_to_book.value == Decimal("0.3")
    assert merged.price_to_book.as_of == date(2026, 7, 10)
    assert merged.price_to_book.sources == ["fundamentus", "status_invest"]
    assert "cvm" not in merged.price_to_book.source_lineage


def _dividend(ex_date: date, value: str) -> Dividend:
    return Dividend(
        ex_date=ex_date,
        payment_date=None,
        value=Decimal(value),
        type="Dividend",
        is_future_payment=False,
        is_future_ex_date=False,
        raw={},
    )


def test_distribution_consensus_uses_agreement_and_retains_provenance() -> None:
    ex_date = date(2026, 6, 30)
    reconciled = _reconcile_fund_distributions(
        [_dividend(ex_date, "1.20")],
        (
            FundDistribution(
                ex_date=ex_date,
                value=Decimal("1.20"),
                source="status_invest",
            ),
        ),
    )

    assert reconciled[0].consensus.status is ConsensusStatus.consensus
    assert reconciled[0].consensus.value == Decimal("1.20")
    assert reconciled[0].consensus.independent_sources == (
        "fundamentus",
        "status_invest",
    )
    projected = _merge_fund_distributions(
        [_dividend(ex_date, "1.20")],
        (
            FundDistribution(
                ex_date=ex_date,
                value=Decimal("1.20"),
                source="status_invest",
            ),
        ),
    )
    assert projected == (
        FundDistribution(
            ex_date=ex_date,
            value=Decimal("1.20"),
            source="fundamentus+status_invest",
        ),
    )


def test_distribution_consensus_accepts_one_available_source_explicitly() -> None:
    ex_date = date(2026, 6, 30)
    reconciled = _reconcile_fund_distributions([_dividend(ex_date, "1.20")], ())

    assert reconciled[0].consensus.status is ConsensusStatus.single_source
    assert reconciled[0].consensus.value == Decimal("1.20")
    assert _merge_fund_distributions([_dividend(ex_date, "1.20")], ()) == (
        FundDistribution(
            ex_date=ex_date,
            value=Decimal("1.20"),
            source="fundamentus",
        ),
    )


def test_distribution_consensus_rejects_conflicts_and_outvotes_an_outlier() -> None:
    ex_date = date(2026, 6, 30)
    conflicting = _reconcile_fund_distributions(
        [_dividend(ex_date, "1.00")],
        (
            FundDistribution(
                ex_date=ex_date,
                value=Decimal("1.50"),
                source="status_invest",
            ),
        ),
    )
    assert conflicting[0].consensus.status is ConsensusStatus.conflict
    assert conflicting[0].consensus.value is None
    assert (
        _merge_fund_distributions(
            [_dividend(ex_date, "1.00")],
            (
                FundDistribution(
                    ex_date=ex_date,
                    value=Decimal("1.50"),
                    source="status_invest",
                ),
            ),
        )
        == ()
    )

    outlier = _reconcile_fund_distributions(
        [_dividend(ex_date, "1.00")],
        (
            FundDistribution(
                ex_date=ex_date,
                value=Decimal("1.01"),
                source="status_invest",
            ),
            FundDistribution(
                ex_date=ex_date,
                value=Decimal("1.50"),
                source="cvm",
            ),
        ),
    )[0].consensus
    assert outlier.status is ConsensusStatus.consensus
    assert outlier.value == Decimal("1.005")
    assert [item.source for item in outlier.rejected_observations] == ["cvm"]

    metrics = _add_distribution_metrics(
        _opportunity_metrics(None, [], {}, Decimal("6")),
        conflicting,
    )
    assert metrics.latest_distribution is not None
    assert metrics.latest_distribution.value is None
    assert metrics.latest_distribution.consensus_status == ConsensusStatus.conflict.value
    assert metrics.median_distribution_3m is not None
    assert metrics.median_distribution_3m.value is None
    assert metrics.median_distribution_3m.consensus_status == ConsensusStatus.conflict.value

    newest_conflict = _reconcile_fund_distributions(
        [_dividend(ex_date, "1.00"), _dividend(date(2026, 5, 30), "0.80")],
        (
            FundDistribution(
                ex_date=ex_date,
                value=Decimal("1.50"),
                source="status_invest",
            ),
            FundDistribution(
                ex_date=date(2026, 5, 30),
                value=Decimal("0.80"),
                source="status_invest",
            ),
        ),
    )
    newest_metrics = _add_distribution_metrics(
        _opportunity_metrics(None, [], {}, Decimal("6")),
        newest_conflict,
    )
    assert newest_metrics.latest_distribution is not None
    assert newest_metrics.latest_distribution.value is None
    assert newest_metrics.latest_distribution.consensus_status == ConsensusStatus.conflict.value


def test_distribution_evidence_serializes_conflicts_and_selected_provenance() -> None:
    newest = date(2026, 6, 30)
    older = date(2026, 5, 30)
    reconciled = _reconcile_fund_distributions(
        [_dividend(newest, "1.00"), _dividend(older, "1.00")],
        (
            FundDistribution(ex_date=newest, value=Decimal("1.50"), source="status_invest"),
            FundDistribution(ex_date=older, value=Decimal("1.01"), source="status_invest"),
            FundDistribution(ex_date=older, value=Decimal("1.50"), source="cvm"),
        ),
    )

    evidence = _public_fund_distribution_evidence(reconciled)

    assert [item.ex_date for item in evidence] == [newest, older]
    assert evidence[0].value is None
    assert evidence[0].status == ConsensusStatus.conflict.value
    assert evidence[0].reason == "Independent sources disagree without a clear majority"
    assert {item.source for item in evidence[0].observations} == {
        "fundamentus",
        "status_invest",
    }
    assert evidence[0].rejected_observations == []
    assert evidence[1].value == Decimal("1.005")
    assert evidence[1].status == ConsensusStatus.consensus.value
    assert evidence[1].sources == ["fundamentus", "status_invest"]
    assert [item.source for item in evidence[1].rejected_observations] == ["cvm"]
    payload = evidence[0].model_dump(mode="json")
    assert payload["ex_date"] == "2026-06-30"
    assert payload["value"] is None
    assert payload["status"] == "conflict"
    assert payload["observations"][0]["value"] in {"1.00", "1.50"}


def test_negative_distribution_is_a_rejected_evidence_event_only() -> None:
    ex_date = date(2026, 6, 30)
    reconciled = _reconcile_fund_distributions([_dividend(ex_date, "-1.00")], ())

    assert len(reconciled) == 1
    evidence = _public_fund_distribution_evidence(reconciled)[0]
    assert evidence.value is None
    assert evidence.status == ConsensusStatus.invalid_data.value
    assert evidence.reason == "All observations were outside the plausible range"
    assert [item.value for item in evidence.observations] == [Decimal("-1.00")]
    assert [item.value for item in evidence.rejected_observations] == [Decimal("-1.00")]
    assert _public_fund_distributions(reconciled) == ()


def test_opportunity_metric_exposes_rejected_consensus_observations() -> None:
    result = resolve_consensus(
        [
            SourceObservation(value=Decimal("-1"), source="fundamentus", unit="BRL"),
            SourceObservation(value=Decimal("10"), source="status_invest", unit="BRL"),
        ],
        expected_unit="BRL",
        valid_range=(Decimal("0"), Decimal("100")),
    )

    metric = _metric(
        result.value,
        as_of=result.as_of,
        sources=list(result.sources),
        reason="Current price unavailable",
        unit="BRL",
        consensus=result,
    )

    assert metric.value == Decimal("10")
    assert [item.value for item in metric.observations] == [Decimal("-1"), Decimal("10")]
    assert [item.value for item in metric.rejected_observations] == [Decimal("-1")]


def test_distribution_consensus_excludes_internal_conflicts_per_origin() -> None:
    ex_date = date(2026, 6, 30)
    duplicate = _reconcile_fund_distributions(
        [_dividend(ex_date, "1.00"), _dividend(ex_date, "1.00")],
        (
            FundDistribution(
                ex_date=ex_date,
                value=Decimal("1.00"),
                source="status_invest",
            ),
        ),
    )[0].consensus
    assert duplicate.status is ConsensusStatus.consensus
    assert duplicate.value == Decimal("1.00")
    assert duplicate.independent_sources == ("fundamentus", "status_invest")

    internal_conflict = _reconcile_fund_distributions(
        [_dividend(ex_date, "1.00")],
        (
            FundDistribution(
                ex_date=ex_date,
                value=Decimal("1.00"),
                source="status_invest",
            ),
            FundDistribution(
                ex_date=ex_date,
                value=Decimal("1.50"),
                source="status_invest",
            ),
        ),
    )[0].consensus
    assert internal_conflict.status is ConsensusStatus.single_source
    assert internal_conflict.value == Decimal("1.00")
    assert internal_conflict.independent_sources == ("fundamentus",)
    assert [item.value for item in internal_conflict.rejected_observations] == [
        Decimal("1.00"),
        Decimal("1.50"),
    ]


def test_distribution_consensus_allows_majority_after_excluding_ambiguous_origin() -> None:
    ex_date = date(2026, 6, 30)
    consensus = _reconcile_fund_distributions(
        [_dividend(ex_date, "1.00")],
        (
            FundDistribution(ex_date=ex_date, value=Decimal("1.00"), source="status_invest"),
            FundDistribution(ex_date=ex_date, value=Decimal("1.50"), source="status_invest"),
            FundDistribution(ex_date=ex_date, value=Decimal("1.00"), source="cvm"),
        ),
    )[0].consensus

    assert consensus.status is ConsensusStatus.consensus
    assert consensus.value == Decimal("1.00")
    assert consensus.independent_sources == ("cvm", "fundamentus")
    assert {item.source for item in consensus.rejected_observations} == {"status_invest"}


def test_distribution_consensus_is_independent_of_source_order() -> None:
    dates = [date(2026, 6, 30), date(2026, 5, 30), date(2026, 4, 30)]
    dividends = [_dividend(dates[0], "1.20"), _dividend(dates[1], "1.00")]
    statuses = (
        FundDistribution(ex_date=dates[2], value=Decimal("0.80"), source="status_invest"),
        FundDistribution(ex_date=dates[0], value=Decimal("1.20"), source="status_invest"),
        FundDistribution(ex_date=dates[1], value=Decimal("1.00"), source="status_invest"),
    )
    forward = _reconcile_fund_distributions(dividends, statuses)
    reverse = _reconcile_fund_distributions(list(reversed(dividends)), tuple(reversed(statuses)))

    assert [item.consensus.model_dump(mode="json") for item in forward] == [
        item.consensus.model_dump(mode="json") for item in reverse
    ]
    assert _merge_fund_distributions(dividends, statuses) == _merge_fund_distributions(
        list(reversed(dividends)),
        tuple(reversed(statuses)),
    )


@pytest.mark.asyncio
async def test_opportunity_service_propagates_partial_cvm_archive_failures() -> None:
    class PartialCvmProvider(FakeCvmProvider):
        async def reports(
            self,
            instrument: InstrumentMetadata | None,
            *,
            cnpj: str | None = None,
            today: date | None = None,
        ) -> FundReportSeries:
            series = await super().reports(instrument, cnpj=cnpj, today=today)
            return FundReportSeries(
                cnpj=series.cnpj,
                reports=series.reports,
                archive_failures=(
                    FundArchiveFailure(
                        path="/dados/FII/DOC/INF_MENSAL/DADOS/inf_mensal_fii_2026.zip",
                        code="PROVIDER_UNAVAILABLE",
                    ),
                ),
            )

    service = OpportunityService(
        FakeAssetService(),  # type: ignore[arg-type]
        Settings(),
        b3_provider=FakeB3Provider(),  # type: ignore[arg-type]
        status_provider=FakeStatusProvider(),  # type: ignore[arg-type]
        cvm_provider=PartialCvmProvider(),  # type: ignore[arg-type]
    )

    result = await service.opportunity("TEST11")

    assert result.source_failures == {
        "cvm:/dados/FII/DOC/INF_MENSAL/DADOS/inf_mensal_fii_2026.zip": "PROVIDER_UNAVAILABLE",
    }


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
async def test_status_invest_provider_does_not_cache_transport_failures() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    provider = StatusInvestProvider(Settings(), httpx.MockTransport(handler))

    with pytest.raises(ProviderUnavailableError):
        await provider.profile("TEST3", InstrumentType.stock)
    with pytest.raises(ProviderUnavailableError):
        await provider.profile("TEST3", InstrumentType.stock)
    assert calls == 2


@pytest.mark.asyncio
async def test_status_invest_provider_caches_confirmed_missing_profile() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404)

    provider = StatusInvestProvider(Settings(), httpx.MockTransport(handler))

    assert await provider.profile("TEST3", InstrumentType.stock) == StatusInvestProfile(values={})
    assert await provider.profile("TEST3", InstrumentType.stock) == StatusInvestProfile(values={})
    assert calls == 1


@pytest.mark.asyncio
async def test_status_invest_provider_caches_confirmed_empty_profiles() -> None:
    calls = 0
    empty_html = "<html><body>profile has no published indicators</body></html>"

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text=empty_html)

    provider = StatusInvestProvider(Settings(), httpx.MockTransport(handler))

    assert await provider.profile("TEST3", None) == StatusInvestProfile(values={})
    assert await provider.profile("TEST3", None) == StatusInvestProfile(values={})
    # An unknown instrument type tries every supported route before a negative
    # result is considered confirmed and cached.
    assert calls == 4


@pytest.mark.asyncio
async def test_status_invest_provider_retries_after_empty_profile_and_outage() -> None:
    calls = 0
    empty_html = "<html><body>profile has no published indicators</body></html>"

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1 or calls >= 5:
            return httpx.Response(200, text=empty_html)
        return httpx.Response(503)

    provider = StatusInvestProvider(Settings(), httpx.MockTransport(handler))

    with pytest.raises(ProviderUnavailableError):
        await provider.profile("TEST3", None)
    assert calls == 4

    # The first partial response set was not cached. The next request retries
    # every route and can establish a confirmed empty result.
    assert await provider.profile("TEST3", None) == StatusInvestProfile(values={})
    assert calls == 8
    assert await provider.profile("TEST3", None) == StatusInvestProfile(values={})
    assert calls == 8


@pytest.mark.asyncio
async def test_status_invest_provider_rejects_empty_html_as_invalid_response() -> None:
    provider = StatusInvestProvider(
        Settings(),
        httpx.MockTransport(lambda _request: httpx.Response(200, text="")),
    )

    with pytest.raises(ProviderInvalidResponseError):
        await provider.profile("TEST3", InstrumentType.stock)


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
async def test_b3_provider_distinguishes_absence_from_transport_failure() -> None:
    unavailable = B3InstrumentProvider(
        Settings(),
        httpx.MockTransport(lambda _request: httpx.Response(503)),
    )

    with pytest.raises(ProviderUnavailableError):
        await unavailable.get("TEST3")
    assert unavailable.cached(["TEST3"]) == []

    missing = B3InstrumentProvider(
        Settings(),
        httpx.MockTransport(lambda _request: httpx.Response(404)),
    )
    assert await missing.get("TEST3") is None
    assert await missing.get("TEST3") is None


async def test_verified_b3_listing_supplies_issuer_identity_when_bulletin_omits_it() -> None:
    provider = B3InstrumentProvider(
        Settings(),
        httpx.MockTransport(lambda _request: pytest.fail("verified listing must not query BDI")),
    )

    instrument = await provider.get("B3SA3")

    assert instrument is not None
    assert instrument.isin == "BRB3SAACNOR6"
    assert instrument.identifiers["cnpj"] == "09346601000125"
    assert instrument.name == "B3 S.A. - BRASIL, BOLSA, BALCÃO"
    assert instrument.source == "b3"
    assert instrument.confidence == "verified"
    assert provider.cached(["B3SA3"]) == [instrument]


def test_verified_b3sa3_listing_selects_its_cvm_issuer() -> None:
    from app.assessment.service import _trusted_b3_name
    from app.services.company_matching import match_company
    from app.services.opportunity import _VERIFIED_B3_LISTINGS

    name = _trusted_b3_name("B3SA3", _VERIFIED_B3_LISTINGS["B3SA3"])
    assert name is not None

    match = match_company(
        name,
        {
            "09346601000125": "B3 S.A. - BRASIL, BOLSA, BALCÃO",
            "00000000000000": "BANCO DO BRASIL S.A.",
        },
    )

    assert match is not None
    assert match.cnpj == "09346601000125"
    assert match.confidence == "high"


@pytest.mark.asyncio
async def test_b3_provider_caches_confirmed_empty_bulletins() -> None:
    calls = 0
    empty_payload = {"table": {"columns": [{"name": "TckrSymb"}], "values": []}}

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=empty_payload)

    provider = B3InstrumentProvider(Settings(), httpx.MockTransport(handler))

    assert await provider.get("TEST3") is None
    assert await provider.get("TEST3") is None
    # The seven successful date lookups establish that the empty bulletin is
    # safe to cache; the second call is served entirely from that cache.
    assert calls == 7


@pytest.mark.asyncio
async def test_b3_provider_retries_after_empty_bulletin_and_outage() -> None:
    calls = 0
    empty_payload = {"table": {"columns": [{"name": "TckrSymb"}], "values": []}}

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1 or calls >= 8:
            return httpx.Response(200, json=empty_payload)
        return httpx.Response(503)

    provider = B3InstrumentProvider(Settings(), httpx.MockTransport(handler))

    with pytest.raises(ProviderUnavailableError):
        await provider.get("TEST3")
    assert calls == 7

    # A later healthy pass is retried and only then is the empty result cached.
    assert await provider.get("TEST3") is None
    assert calls == 14
    assert await provider.get("TEST3") is None
    assert calls == 14


@pytest.mark.asyncio
async def test_b3_provider_rejects_schema_failures_without_negative_caching() -> None:
    provider = B3InstrumentProvider(
        Settings(),
        httpx.MockTransport(lambda _request: httpx.Response(200, json={"unexpected": []})),
    )

    with pytest.raises(ProviderInvalidResponseError):
        await provider.get("TEST3")
    assert provider.cached(["TEST3"]) == []


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


@pytest.mark.asyncio
async def test_market_providers_classify_transport_and_schema_failures() -> None:
    def request_failure(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=_request)

    unavailable_b3 = B3InstrumentProvider(
        Settings(),
        httpx.MockTransport(request_failure),
    )
    with pytest.raises(ProviderUnavailableError):
        await unavailable_b3.get("TEST3")

    invalid_b3 = B3InstrumentProvider(
        Settings(),
        httpx.MockTransport(lambda _request: httpx.Response(200, content=b"not-json")),
    )
    with pytest.raises(ProviderInvalidResponseError):
        await invalid_b3.get("TEST3")

    unavailable_status = StatusInvestProvider(
        Settings(),
        httpx.MockTransport(request_failure),
    )
    with pytest.raises(ProviderUnavailableError):
        await unavailable_status.profile("TEST3", InstrumentType.stock)


def test_b3_payload_requires_a_nonempty_column_schema() -> None:
    assert not _is_valid_b3_payload({"table": {"columns": [], "values": []}})
    assert not _is_valid_b3_payload({"table": {"columns": [{}], "values": []}})
    assert not _is_valid_b3_payload({"table": {"columns": None, "values": []}})


def test_opportunity_metrics_accept_status_invest_dividend_observation() -> None:
    metrics = _opportunity_metrics(
        None,
        [],
        {"dividends_12m": Decimal("1.25")},
        Decimal("6"),
    )

    assert metrics.dividends_12m.value == Decimal("1.25")
    assert metrics.dividends_12m.sources == ["status_invest"]


def test_empty_fundamentus_dividend_feed_does_not_outvote_positive_observation() -> None:
    details = AssetDetails(
        ticker="TEST3",
        quote=Decimal("10"),
        quote_date=date(2026, 7, 10),
        sections=[],
        source_url="https://example.test",
        scraped_at=datetime(2026, 7, 10, tzinfo=UTC),
    )

    metrics = _opportunity_metrics(
        details,
        [],
        {"dividends_12m": Decimal("10")},
        Decimal("6"),
    )

    assert metrics.dividends_12m.value == Decimal("10")
    assert metrics.dividends_12m.sources == ["status_invest"]
    assert metrics.dividend_yield_12m.value == Decimal("100")
    assert metrics.bazin_price.value == Decimal("166.6666666666666666666666667")


@pytest.mark.asyncio
async def test_opportunity_service_rethrows_invalid_identity_errors() -> None:
    class InvalidB3Provider(FakeB3Provider):
        async def get(self, ticker: str) -> InstrumentMetadata:
            raise InvalidTickerError(ticker=ticker)

    service = OpportunityService(
        FakeAssetService(),  # type: ignore[arg-type]
        Settings(),
        b3_provider=InvalidB3Provider(),  # type: ignore[arg-type]
        status_provider=FakeStatusProvider(),  # type: ignore[arg-type]
    )

    with pytest.raises(InvalidTickerError):
        await service.opportunity("BAD3")


@pytest.mark.asyncio
async def test_opportunity_service_preserves_independent_source_failures() -> None:
    class FailedAssetService(FakeAssetService):
        async def get_asset(self, ticker: str) -> AssetResponse:
            raise ProviderUnavailableError(ticker=ticker)

    class FailedCvmProvider(FakeCvmProvider):
        async def reports(
            self,
            instrument: InstrumentMetadata | None,
            *,
            cnpj: str | None = None,
            today: date | None = None,
        ) -> FundReportSeries:
            raise ProviderInvalidResponseError(ticker=instrument.ticker if instrument else None)

    service = OpportunityService(
        FailedAssetService(),  # type: ignore[arg-type]
        Settings(),
        b3_provider=FakeB3Provider(),  # type: ignore[arg-type]
        status_provider=FakeStatusProvider(),  # type: ignore[arg-type]
        cvm_provider=FailedCvmProvider(),  # type: ignore[arg-type]
    )

    result = await service.opportunity("TEST3")

    assert result.source_failures == {
        "fundamentus": "PROVIDER_UNAVAILABLE",
        "cvm": "PROVIDER_INVALID_RESPONSE",
    }
