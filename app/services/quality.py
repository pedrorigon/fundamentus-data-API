from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from app.core.errors import APIError
from app.models import (
    FinancialPeriod,
    FundamentalsSnapshot,
    FundDistribution,
    FundMonthlyReport,
    InstrumentDataResponse,
    InstrumentType,
    OpportunityResponse,
)
from app.models.quality import (
    QualityAssetFacts,
    QualityAssetKind,
    QualityAssetRequest,
    QualityFact,
    QualityFactObservation,
    QualityFactsRequest,
    QualityFactsResponse,
)
from app.services.fundamentals import FundamentalsService
from app.services.fundamentals_math import is_financial_sector
from app.services.market import InstrumentDataService
from app.services.opportunity import OpportunityService

_MAX_CONCURRENCY = 6
_CVM_SOURCE = "cvm"
_CVM_CONFIDENCE = Decimal("0.95")
_DERIVED_CONFIDENCE = Decimal("0.90")
_FCF_CONFIDENCE = Decimal("0.65")


class QualityFactsService:
    """Resolve normalized quality evidence in bounded portfolio-sized batches."""

    def __init__(
        self,
        fundamentals: FundamentalsService,
        instruments: InstrumentDataService,
        opportunity: OpportunityService,
    ) -> None:
        self.fundamentals = fundamentals
        self.instruments = instruments
        self.opportunity = opportunity
        self._semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)

    async def resolve(self, request: QualityFactsRequest) -> QualityFactsResponse:
        assets = await asyncio.gather(*(self._bounded(asset) for asset in request.assets))
        return QualityFactsResponse(assets=list(assets), refreshed_at=datetime.now(UTC))

    async def _bounded(self, asset: QualityAssetRequest) -> QualityAssetFacts:
        async with self._semaphore:
            try:
                return await self._resolve_asset(asset)
            except APIError as error:
                return QualityAssetFacts(
                    ticker=asset.ticker,
                    kind=asset.kind,
                    unavailable_reason=error.message,
                )

    async def _resolve_asset(self, asset: QualityAssetRequest) -> QualityAssetFacts:
        if asset.kind is QualityAssetKind.crypto:
            return _unsupported(asset, "Use a network-data provider for cryptocurrency quality")
        if asset.kind is QualityAssetKind.fixed_income:
            return _unsupported(asset, "Use issuer and instrument identifiers for credit quality")

        instrument_type = _instrument_type(asset.kind)
        instrument_data, opportunity = await asyncio.gather(
            self.instruments.get(asset.ticker, instrument_type),
            self.opportunity.opportunity(asset.ticker),
        )
        if asset.kind is QualityAssetKind.etf:
            return _etf_facts(asset, instrument_data)
        if asset.kind is QualityAssetKind.real_estate_fund:
            return _fund_facts(asset, opportunity)
        return await self._stock_facts(asset, instrument_data, opportunity)

    async def _stock_facts(
        self,
        asset: QualityAssetRequest,
        instrument_data: InstrumentDataResponse,
        opportunity: OpportunityResponse,
    ) -> QualityAssetFacts:
        instrument = opportunity.instrument or instrument_data.instrument
        if instrument is None or instrument.category == "INTERNATIONAL":
            return _international_stock_facts(asset, instrument_data)
        metrics = opportunity.metrics
        snapshot = await self.fundamentals.snapshot(
            asset.ticker,
            instrument.name,
            reference_shares=metrics.shares_outstanding.value,
            earnings_per_share=metrics.earnings_per_share.value,
            book_value_per_share=metrics.book_value_per_share.value,
            recurring_dividends_per_share=metrics.dividends_12m.value,
            supplemental_sources={
                "earnings_per_share": ",".join(metrics.earnings_per_share.sources),
                "book_value_per_share": ",".join(metrics.book_value_per_share.sources),
                "recurring_dividends_per_share": ",".join(metrics.dividends_12m.sources),
            },
        )
        return _financial_facts(asset, snapshot, instrument.isin)


def _instrument_type(kind: QualityAssetKind) -> InstrumentType:
    return {
        QualityAssetKind.stock: InstrumentType.stock,
        QualityAssetKind.real_estate_fund: InstrumentType.fii,
        QualityAssetKind.etf: InstrumentType.etf,
    }.get(kind, InstrumentType.unknown)


def _financial_facts(
    request: QualityAssetRequest,
    snapshot: FundamentalsSnapshot,
    isin: str | None,
) -> QualityAssetFacts:
    period = snapshot.trailing_twelve_months
    if period is None:
        return QualityAssetFacts(
            ticker=request.ticker,
            kind=request.kind,
            canonical_id=snapshot.cnpj or isin,
            unavailable_reason=snapshot.unavailable_reason or "No comparable financial period",
        )
    reference = period.period_end
    annual = sorted(
        (item for item in snapshot.periods if item.annual),
        key=lambda item: item.period_end,
    )
    reference_shares = snapshot.shares_outstanding or period.shares_outstanding
    financial_profile = is_financial_sector(snapshot.sector)
    facts = [
        _ratio("gross_margin", period.gross_profit, period.revenue, reference),
        _ratio("operating_margin", period.ebit, period.revenue, reference),
        _ratio("net_margin", period.net_income, period.revenue, reference),
        _ratio("return_on_equity", period.net_income, period.equity, reference),
        _ratio("return_on_assets", period.net_income, period.total_assets, reference),
        _ratio(
            "cash_conversion",
            period.operating_cash_flow,
            period.net_income,
            reference,
        ),
        _ratio(
            "operating_cash_flow_margin",
            period.operating_cash_flow,
            period.revenue,
            reference,
        ),
        _ratio(
            "accrual_ratio",
            _subtract(period.net_income, period.operating_cash_flow),
            period.total_assets,
            reference,
        ),
        _ratio(
            "free_cash_flow_margin",
            period.free_cash_flow,
            period.revenue,
            reference,
            confidence=_FCF_CONFIDENCE,
        ),
        _ratio("net_debt_to_ebitda", period.net_debt, period.ebitda, reference, unit="multiple"),
        _ratio("debt_to_equity", period.gross_debt, period.equity, reference, unit="multiple"),
        _ratio("equity_ratio", period.equity, period.total_assets, reference),
        _ratio(
            "current_ratio",
            period.current_assets,
            period.current_liabilities,
            reference,
            unit="multiple",
        ),
        _interest_coverage(period.ebit, period.financial_result, reference),
        _growth_fact("revenue_cagr", annual, lambda item: item.revenue),
        _growth_fact("earnings_cagr", annual, lambda item: item.net_income),
        _stability_fact(
            "net_margin_volatility",
            annual,
            lambda item: _safe_ratio(item.net_income, item.revenue),
        ),
        _stability_fact(
            "return_on_equity_volatility",
            annual,
            lambda item: _safe_ratio(item.net_income, item.equity),
        ),
        _positive_frequency("positive_earnings_frequency", annual, lambda item: item.net_income),
        _positive_frequency(
            "positive_free_cash_flow_frequency",
            annual,
            lambda item: item.free_cash_flow,
        ),
        _growth_fact("share_dilution", annual, lambda item: item.shares_outstanding),
        _consistency_fact(
            "earnings_per_share_consistency_error",
            _safe_ratio(period.net_income, reference_shares),
            snapshot.earnings_per_share,
            reference,
        ),
        _consistency_fact(
            "book_value_per_share_consistency_error",
            _safe_ratio(period.equity, reference_shares),
            snapshot.book_value_per_share,
            reference,
        ),
    ]
    facts, warnings = _validated_financial_facts(facts)
    if financial_profile:
        warnings.append(
            "Bank and insurer capital adequacy and credit-loss facts require a BCB data source"
        )
    sources = sorted(
        {item.selected_source for item in snapshot.provenance if item.selected_source is not None}
        | {_CVM_SOURCE}
    )
    return QualityAssetFacts(
        ticker=request.ticker,
        kind=request.kind,
        canonical_id=snapshot.cnpj or isin,
        profile="financial" if financial_profile else "industrial",
        facts=facts,
        sources=sources,
        warnings=warnings,
    )


def _international_stock_facts(
    request: QualityAssetRequest,
    data: InstrumentDataResponse,
) -> QualityAssetFacts:
    fundamentals = data.fundamentals
    if fundamentals is None:
        return QualityAssetFacts(
            ticker=request.ticker,
            kind=request.kind,
            canonical_id=data.instrument.isin if data.instrument else None,
            unavailable_reason="No public international fundamentals were resolved",
        )
    reference = data.refreshed_at.date()
    facts = [
        _value_fact(
            "market_capitalization",
            fundamentals.market_capitalization,
            "currency",
            reference,
            fundamentals.source,
        ),
        _value_fact(
            "dividend_yield",
            fundamentals.dividend_yield,
            "ratio",
            reference,
            fundamentals.source,
        ),
    ]
    return QualityAssetFacts(
        ticker=request.ticker,
        kind=request.kind,
        canonical_id=data.instrument.isin if data.instrument else None,
        facts=facts,
        sources=[fundamentals.source],
        warnings=["Public international profitability history is unavailable"],
    )


def _etf_facts(
    request: QualityAssetRequest,
    data: InstrumentDataResponse,
) -> QualityAssetFacts:
    profile = data.fund_profile
    if profile is None:
        return QualityAssetFacts(
            ticker=request.ticker,
            kind=request.kind,
            canonical_id=data.instrument.isin if data.instrument else None,
            unavailable_reason="No public fund profile was resolved",
        )
    reference = data.refreshed_at.date()
    holdings = profile.holdings
    total_weight = sum((holding.weight for holding in holdings), Decimal("0"))
    weight_scale = Decimal("100") if total_weight > Decimal("1.5") else Decimal("1")
    facts = [
        _value_fact(
            "expense_ratio",
            profile.net_expense_ratio,
            "ratio",
            reference,
            profile.source,
        ),
        _value_fact(
            "portfolio_turnover",
            profile.portfolio_turnover,
            "ratio",
            reference,
            profile.source,
        ),
        _value_fact(
            "net_assets",
            profile.net_assets,
            "currency",
            reference,
            profile.source,
        ),
        _value_fact(
            "fund_age_years",
            _age_years(profile.inception_date, reference),
            "years",
            reference,
            profile.source,
        ),
        _value_fact(
            "holdings_count",
            Decimal(len(holdings)) if holdings else None,
            "count",
            reference,
            profile.source,
        ),
        _value_fact(
            "top_ten_concentration",
            sum((holding.weight for holding in holdings[:10]), Decimal("0")) if holdings else None,
            "ratio",
            reference,
            profile.source,
        ),
        _value_fact(
            "holdings_hhi",
            _hhi(holding.weight for holding in holdings),
            "ratio",
            reference,
            profile.source,
        ),
        _value_fact(
            "sector_hhi",
            _hhi(allocation.weight for allocation in profile.sectors),
            "ratio",
            reference,
            profile.source,
        ),
        _value_fact(
            "holdings_weight_coverage",
            _safe_ratio(total_weight, weight_scale) if holdings else None,
            "ratio",
            reference,
            profile.source,
        ),
    ]
    return QualityAssetFacts(
        ticker=request.ticker,
        kind=request.kind,
        canonical_id=data.instrument.isin if data.instrument else None,
        facts=facts,
        sources=[profile.source],
    )


def _fund_facts(
    request: QualityAssetRequest,
    opportunity: OpportunityResponse,
) -> QualityAssetFacts:
    reports = sorted(
        opportunity.fund_reports.reports if opportunity.fund_reports else [],
        key=lambda item: item.as_of,
    )
    distributions = sorted(
        opportunity.fund_distributions,
        key=lambda item: item.ex_date,
    )
    source = _CVM_SOURCE
    distribution_consistency = _distribution_report_consistency(reports, distributions)
    facts = [
        _value_fact(
            "reporting_history_months",
            Decimal(len(reports)) if reports else None,
            "count",
            reports[-1].as_of if reports else None,
            source,
        ),
        _value_fact(
            "distribution_history_months",
            Decimal(len(distributions)) if distributions else None,
            "count",
            distributions[-1].ex_date if distributions else None,
            source,
        ),
        _distribution_stability(distributions),
        _positive_distribution_frequency(distributions),
        _distribution_growth(distributions),
        _distribution_cut_frequency(distributions),
        _reporting_regularity(reports),
        _report_completeness(reports),
        _nav_growth(reports),
        _nav_return_volatility(reports),
        _positive_nav_return_frequency(reports),
        _nav_max_drawdown(reports),
        distribution_consistency,
    ]
    instrument = opportunity.instrument
    warnings = (
        ["Distribution values diverge from the corresponding CVM monthly reports"]
        if distribution_consistency.value is not None
        and distribution_consistency.value > Decimal("0.25")
        else []
    )
    return QualityAssetFacts(
        ticker=request.ticker,
        kind=request.kind,
        canonical_id=instrument.isin if instrument else None,
        facts=facts,
        sources=[source] if reports or distributions else [],
        warnings=warnings,
        unavailable_reason=None
        if reports or distributions
        else "No public fund history was resolved",
    )


def _ratio(
    key: str,
    numerator: Decimal | None,
    denominator: Decimal | None,
    reference: date,
    *,
    unit: str = "ratio",
    confidence: Decimal = _CVM_CONFIDENCE,
) -> QualityFact:
    value = _safe_ratio(numerator, denominator)
    return _value_fact(key, value, unit, reference, _CVM_SOURCE, confidence=confidence)


def _interest_coverage(
    ebit: Decimal | None,
    financial_result: Decimal | None,
    reference: date,
) -> QualityFact:
    expense = (
        abs(financial_result) if financial_result is not None and financial_result < 0 else None
    )
    return _ratio("interest_coverage", ebit, expense, reference, unit="multiple")


def _growth_fact(
    key: str,
    periods: list[FinancialPeriod],
    getter: Callable[[FinancialPeriod], Decimal | None],
) -> QualityFact:
    observations = _observations(periods, getter)
    if len(observations) < 3:
        return _missing_fact(key, "ratio", "At least three annual observations are required")
    first, last = observations[0], observations[-1]
    years = Decimal(str((last.as_of - first.as_of).days / 365.25))
    value = _annualized_growth(first.value, last.value, years)
    return QualityFact(
        key=key,
        value=value,
        unit="ratio",
        as_of=last.as_of,
        source=_CVM_SOURCE if value is not None else None,
        confidence=_DERIVED_CONFIDENCE if value is not None else Decimal("0"),
        status="valid" if value is not None else "missing_data",
        unavailable_reason=None if value is not None else "Growth needs positive comparable values",
        history=observations,
    )


def _positive_frequency(
    key: str,
    periods: list[FinancialPeriod],
    getter: Callable[[FinancialPeriod], Decimal | None],
) -> QualityFact:
    observations = _observations(periods, getter)
    if len(observations) < 3:
        return _missing_fact(key, "ratio", "At least three annual observations are required")
    positives = sum(1 for item in observations if item.value > 0)
    return QualityFact(
        key=key,
        value=Decimal(positives) / Decimal(len(observations)),
        unit="ratio",
        as_of=observations[-1].as_of,
        source=_CVM_SOURCE,
        confidence=_DERIVED_CONFIDENCE,
        status="valid",
        history=observations,
    )


def _observations(
    periods: Iterable[FinancialPeriod],
    getter: Callable[[FinancialPeriod], Decimal | None],
) -> list[QualityFactObservation]:
    return [
        QualityFactObservation(as_of=period.period_end, value=value)
        for period in periods
        if (value := getter(period)) is not None
    ]


def _stability_fact(
    key: str,
    periods: list[FinancialPeriod],
    getter: Callable[[FinancialPeriod], Decimal | None],
) -> QualityFact:
    observations = _observations(periods, getter)
    if len(observations) < 3:
        return _missing_fact(key, "ratio", "At least three annual observations are required")
    values = [item.value for item in observations]
    average = sum(values, Decimal("0")) / Decimal(len(values))
    variance = sum(((value - average) ** 2 for value in values), Decimal("0")) / Decimal(
        len(values)
    )
    volatility = Decimal(str(float(variance) ** 0.5))
    return QualityFact(
        key=key,
        value=volatility,
        unit="ratio",
        as_of=observations[-1].as_of,
        source=_CVM_SOURCE,
        confidence=_DERIVED_CONFIDENCE,
        status="valid",
        history=observations,
    )


def _consistency_fact(
    key: str,
    filing_value: Decimal | None,
    public_value: Decimal | None,
    reference: date,
) -> QualityFact:
    if filing_value is None or public_value is None:
        return _missing_fact(
            key,
            "ratio",
            "Both CVM totals and independent per-share data are required",
        )
    denominator = max(abs(filing_value), abs(public_value), Decimal("0.000001"))
    error = abs(filing_value - public_value) / denominator
    return _value_fact(
        key,
        error,
        "ratio",
        reference,
        "cvm,fundamentus",
        confidence=_DERIVED_CONFIDENCE,
    )


_FINANCIAL_RANGES: dict[str, tuple[Decimal, Decimal]] = {
    "gross_margin": (Decimal("-0.5"), Decimal("1.2")),
    "operating_margin": (Decimal("-1"), Decimal("1.2")),
    "net_margin": (Decimal("-1"), Decimal("1.2")),
    "return_on_equity": (Decimal("-2"), Decimal("3")),
    "return_on_assets": (Decimal("-1"), Decimal("1")),
    "cash_conversion": (Decimal("-5"), Decimal("8")),
    "operating_cash_flow_margin": (Decimal("-1"), Decimal("1")),
    "accrual_ratio": (Decimal("-1"), Decimal("1")),
    "free_cash_flow_margin": (Decimal("-1"), Decimal("1")),
    "net_debt_to_ebitda": (Decimal("-10"), Decimal("15")),
    "debt_to_equity": (Decimal("0"), Decimal("20")),
    "equity_ratio": (Decimal("-0.2"), Decimal("1.2")),
    "current_ratio": (Decimal("0"), Decimal("20")),
    "interest_coverage": (Decimal("-10"), Decimal("100")),
    "revenue_cagr": (Decimal("-0.8"), Decimal("2")),
    "earnings_cagr": (Decimal("-0.8"), Decimal("2")),
    "share_dilution": (Decimal("-0.5"), Decimal("0.5")),
}


def _validated_financial_facts(
    facts: list[QualityFact],
) -> tuple[list[QualityFact], list[str]]:
    validated: list[QualityFact] = []
    warnings: list[str] = []
    for fact in facts:
        bounds = _FINANCIAL_RANGES.get(fact.key)
        if fact.value is None or bounds is None or bounds[0] <= fact.value <= bounds[1]:
            validated.append(fact)
            continue
        warnings.append(
            f"{fact.key} was rejected as implausible ({fact.value}); "
            f"expected {bounds[0]}..{bounds[1]}"
        )
        validated.append(
            _missing_fact(
                fact.key,
                fact.unit,
                "Public values failed the plausibility cross-check",
            )
        )
    return validated, warnings


def _distribution_stability(distributions: list[FundDistribution]) -> QualityFact:
    values = [item.value for item in distributions[-36:] if item.value >= 0]
    if len(values) < 6:
        return _missing_fact(
            "distribution_stability",
            "ratio",
            "At least six distributions are required",
        )
    average = sum(values, Decimal("0")) / Decimal(len(values))
    variance = sum(((value - average) ** 2 for value in values), Decimal("0")) / Decimal(
        len(values)
    )
    deviation = Decimal(str(float(variance) ** 0.5))
    return _value_fact(
        "distribution_stability",
        _safe_ratio(deviation, average),
        "ratio",
        distributions[-1].ex_date,
        _CVM_SOURCE,
    )


def _positive_distribution_frequency(distributions: list[FundDistribution]) -> QualityFact:
    recent = distributions[-36:]
    if len(recent) < 6:
        return _missing_fact(
            "positive_distribution_frequency",
            "ratio",
            "At least six distributions are required",
        )
    positive = sum(1 for item in recent if item.value > 0)
    return _value_fact(
        "positive_distribution_frequency",
        Decimal(positive) / Decimal(len(recent)),
        "ratio",
        recent[-1].ex_date,
        _CVM_SOURCE,
    )


def _distribution_growth(distributions: list[FundDistribution]) -> QualityFact:
    recent = distributions[-36:]
    if len(recent) < 12:
        return _missing_fact(
            "distribution_growth",
            "ratio",
            "At least twelve distributions are required",
        )
    midpoint = len(recent) // 2
    earlier = sum((item.value for item in recent[:midpoint]), Decimal("0")) / Decimal(midpoint)
    later_count = len(recent) - midpoint
    later = sum((item.value for item in recent[midpoint:]), Decimal("0")) / Decimal(later_count)
    return _value_fact(
        "distribution_growth",
        _safe_ratio(later - earlier, earlier),
        "ratio",
        recent[-1].ex_date,
        _CVM_SOURCE,
        confidence=_DERIVED_CONFIDENCE,
    )


def _distribution_cut_frequency(distributions: list[FundDistribution]) -> QualityFact:
    recent = distributions[-36:]
    if len(recent) < 6:
        return _missing_fact(
            "distribution_cut_frequency",
            "ratio",
            "At least six distributions are required",
        )
    comparable = [
        (previous.value, current.value)
        for previous, current in zip(recent, recent[1:], strict=False)
        if previous.value > 0
    ]
    if not comparable:
        return _missing_fact(
            "distribution_cut_frequency",
            "ratio",
            "No positive prior distribution is available",
        )
    cuts = sum(1 for previous, current in comparable if current < previous * Decimal("0.90"))
    return _value_fact(
        "distribution_cut_frequency",
        Decimal(cuts) / Decimal(len(comparable)),
        "ratio",
        recent[-1].ex_date,
        _CVM_SOURCE,
        confidence=_DERIVED_CONFIDENCE,
    )


def _reporting_regularity(reports: list[FundMonthlyReport]) -> QualityFact:
    recent = reports[-36:]
    if len(recent) < 6:
        return _missing_fact(
            "reporting_regularity",
            "ratio",
            "At least six monthly reports are required",
        )
    months = {item.as_of.year * 12 + item.as_of.month for item in recent}
    span = max(months) - min(months) + 1
    return _value_fact(
        "reporting_regularity",
        Decimal(len(months)) / Decimal(span),
        "ratio",
        recent[-1].as_of,
        _CVM_SOURCE,
        confidence=_DERIVED_CONFIDENCE,
    )


def _report_completeness(reports: list[FundMonthlyReport]) -> QualityFact:
    recent = reports[-36:]
    if len(recent) < 6:
        return _missing_fact(
            "report_completeness",
            "ratio",
            "At least six monthly reports are required",
        )
    completed = sum(
        1
        for item in recent
        if item.monthly_nav_return is not None and item.monthly_distribution_yield is not None
    )
    return _value_fact(
        "report_completeness",
        Decimal(completed) / Decimal(len(recent)),
        "ratio",
        recent[-1].as_of,
        _CVM_SOURCE,
        confidence=_DERIVED_CONFIDENCE,
    )


def _nav_growth(reports: list[FundMonthlyReport]) -> QualityFact:
    recent = [item for item in reports[-60:] if item.nav_per_share > 0]
    if len(recent) < 12:
        return _missing_fact("nav_growth", "ratio", "At least twelve monthly reports are required")
    years = Decimal(str((recent[-1].as_of - recent[0].as_of).days / 365.25))
    return _value_fact(
        "nav_growth",
        _annualized_growth(recent[0].nav_per_share, recent[-1].nav_per_share, years),
        "ratio",
        recent[-1].as_of,
        _CVM_SOURCE,
        confidence=_DERIVED_CONFIDENCE,
    )


def _nav_return_volatility(reports: list[FundMonthlyReport]) -> QualityFact:
    observations = [item for item in reports[-36:] if item.monthly_nav_return is not None]
    if len(observations) < 6:
        return _missing_fact(
            "nav_return_volatility",
            "ratio",
            "At least six monthly NAV returns are required",
        )
    values = [value for item in observations if (value := item.monthly_nav_return) is not None]
    average = sum(values, Decimal("0")) / Decimal(len(values))
    variance = sum(((value - average) ** 2 for value in values), Decimal("0")) / Decimal(
        len(values)
    )
    return _value_fact(
        "nav_return_volatility",
        Decimal(str(float(variance) ** 0.5)),
        "ratio",
        observations[-1].as_of,
        _CVM_SOURCE,
        confidence=_DERIVED_CONFIDENCE,
    )


def _positive_nav_return_frequency(reports: list[FundMonthlyReport]) -> QualityFact:
    observations = [item for item in reports[-36:] if item.monthly_nav_return is not None]
    if len(observations) < 6:
        return _missing_fact(
            "positive_nav_return_frequency",
            "ratio",
            "At least six monthly NAV returns are required",
        )
    positive = sum(
        1 for item in observations if (value := item.monthly_nav_return) is not None and value >= 0
    )
    return _value_fact(
        "positive_nav_return_frequency",
        Decimal(positive) / Decimal(len(observations)),
        "ratio",
        observations[-1].as_of,
        _CVM_SOURCE,
        confidence=_DERIVED_CONFIDENCE,
    )


def _nav_max_drawdown(reports: list[FundMonthlyReport]) -> QualityFact:
    recent = [item for item in reports[-60:] if item.nav_per_share > 0]
    if len(recent) < 6:
        return _missing_fact(
            "nav_max_drawdown",
            "ratio",
            "At least six monthly reports are required",
        )
    peak = recent[0].nav_per_share
    drawdown = Decimal("0")
    for item in recent:
        peak = max(peak, item.nav_per_share)
        drawdown = max(drawdown, (peak - item.nav_per_share) / peak)
    return _value_fact(
        "nav_max_drawdown",
        drawdown,
        "ratio",
        recent[-1].as_of,
        _CVM_SOURCE,
        confidence=_DERIVED_CONFIDENCE,
    )


def _distribution_report_consistency(
    reports: list[FundMonthlyReport],
    distributions: list[FundDistribution],
) -> QualityFact:
    report_values = {
        (item.as_of.year, item.as_of.month): item.nav_per_share * distribution_yield
        for item in reports[-36:]
        if (distribution_yield := item.monthly_distribution_yield) is not None
        and item.nav_per_share > 0
    }
    errors = [
        abs(item.value - expected) / max(abs(item.value), abs(expected), Decimal("0.000001"))
        for item in distributions[-36:]
        if (expected := report_values.get((item.ex_date.year, item.ex_date.month))) is not None
    ]
    if len(errors) < 6:
        return _missing_fact(
            "distribution_report_consistency_error",
            "ratio",
            "At least six matching distribution and report months are required",
        )
    errors.sort()
    midpoint = len(errors) // 2
    median_error = (
        errors[midpoint]
        if len(errors) % 2
        else (errors[midpoint - 1] + errors[midpoint]) / Decimal("2")
    )
    return _value_fact(
        "distribution_report_consistency_error",
        median_error,
        "ratio",
        reports[-1].as_of,
        "cvm,public_distributions",
        confidence=Decimal("0.85"),
    )


def _value_fact(
    key: str,
    value: Decimal | None,
    unit: str,
    reference: date | None,
    source: str,
    *,
    confidence: Decimal = _CVM_CONFIDENCE,
) -> QualityFact:
    return QualityFact(
        key=key,
        value=value,
        unit=unit,
        as_of=reference if value is not None else None,
        source=source if value is not None else None,
        confidence=confidence if value is not None else Decimal("0"),
        status="valid" if value is not None else "missing_data",
        unavailable_reason=None if value is not None else "Public source did not provide this fact",
    )


def _missing_fact(key: str, unit: str, reason: str) -> QualityFact:
    return QualityFact(key=key, unit=unit, unavailable_reason=reason)


def _safe_ratio(
    numerator: Decimal | None,
    denominator: Decimal | None,
) -> Decimal | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    value = numerator / denominator
    return value if value.is_finite() else None


def _subtract(left: Decimal | None, right: Decimal | None) -> Decimal | None:
    return left - right if left is not None and right is not None else None


def _annualized_growth(
    first: Decimal,
    last: Decimal,
    years: Decimal,
) -> Decimal | None:
    if first <= 0 or last <= 0 or years <= 0:
        return None
    try:
        return Decimal(str(float(last / first) ** (1 / float(years)) - 1))
    except (InvalidOperation, OverflowError, ValueError):
        return None


def _age_years(inception: date | None, reference: date) -> Decimal | None:
    if inception is None or inception > reference:
        return None
    return Decimal(reference.toordinal() - inception.toordinal()) / Decimal("365.25")


def _hhi(weights: Iterable[Decimal]) -> Decimal | None:
    values = list(weights)
    if not values:
        return None
    scale = Decimal("100") if max(values) > 1 else Decimal("1")
    normalized = [value / scale for value in values if value >= 0]
    return sum((value * value for value in normalized), Decimal("0"))


def _unsupported(request: QualityAssetRequest, reason: str) -> QualityAssetFacts:
    return QualityAssetFacts(
        ticker=request.ticker,
        kind=request.kind,
        unavailable_reason=reason,
    )
