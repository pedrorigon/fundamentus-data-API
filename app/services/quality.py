from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from app.models import (
    FinancialPeriod,
    FundamentalsSnapshot,
    FundDistribution,
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
from app.services.market import InstrumentDataService
from app.services.opportunity import OpportunityService

_MAX_CONCURRENCY = 6
_CVM_SOURCE = "cvm"


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
            return await self._resolve_asset(asset)

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
        _ratio("free_cash_flow_margin", period.free_cash_flow, period.revenue, reference),
        _ratio("net_debt_to_ebitda", period.net_debt, period.ebitda, reference, unit="multiple"),
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
        _positive_frequency("positive_earnings_frequency", annual, lambda item: item.net_income),
        _positive_frequency(
            "positive_free_cash_flow_frequency",
            annual,
            lambda item: item.free_cash_flow,
        ),
        _growth_fact("share_dilution", annual, lambda item: item.shares_outstanding),
    ]
    sources = sorted(
        {item.selected_source for item in snapshot.provenance if item.selected_source is not None}
        | {_CVM_SOURCE}
    )
    return QualityAssetFacts(
        ticker=request.ticker,
        kind=request.kind,
        canonical_id=snapshot.cnpj or isin,
        facts=facts,
        sources=sources,
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
    reports = opportunity.fund_reports.reports if opportunity.fund_reports else []
    distributions = opportunity.fund_distributions
    source = _CVM_SOURCE
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
    ]
    instrument = opportunity.instrument
    return QualityAssetFacts(
        ticker=request.ticker,
        kind=request.kind,
        canonical_id=instrument.isin if instrument else None,
        facts=facts,
        sources=[source] if reports or distributions else [],
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
) -> QualityFact:
    value = _safe_ratio(numerator, denominator)
    return _value_fact(key, value, unit, reference, _CVM_SOURCE)


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
        confidence=Decimal("1") if value is not None else Decimal("0"),
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
        confidence=Decimal("1"),
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


def _value_fact(
    key: str,
    value: Decimal | None,
    unit: str,
    reference: date | None,
    source: str,
) -> QualityFact:
    return QualityFact(
        key=key,
        value=value,
        unit=unit,
        as_of=reference if value is not None else None,
        source=source if value is not None else None,
        confidence=Decimal("1") if value is not None else Decimal("0"),
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
