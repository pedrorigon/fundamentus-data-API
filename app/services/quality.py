from __future__ import annotations

import asyncio
import unicodedata
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
from app.models.fundamentals import SectorCompany
from app.models.quality import (
    QualityAssetFacts,
    QualityAssetKind,
    QualityAssetRequest,
    QualityFact,
    QualityFactObservation,
    QualityFactsRequest,
    QualityFactsResponse,
)
from app.scrapers.international_statements import SOURCE_STATEMENTS
from app.services.bcb_quality import (
    BankQualitySnapshot,
    BcbBankProvider,
    BcbMacroProvider,
    MacroQualitySnapshot,
)
from app.services.fundamentals import FundamentalsService
from app.services.market import InstrumentDataService
from app.services.opportunity import OpportunityService

_MAX_CONCURRENCY = 6
_CVM_SOURCE = "cvm"
_CVM_CONFIDENCE = Decimal("0.95")
_DERIVED_CONFIDENCE = Decimal("0.90")
_FCF_CONFIDENCE = Decimal("0.65")
_PEER_CONFIDENCE = Decimal("0.80")
_MARKET_CONFIDENCE = Decimal("0.85")
_BCB_CONFIDENCE = Decimal("0.98")


class QualityFactsService:
    """Resolve normalized quality evidence in bounded portfolio-sized batches."""

    def __init__(
        self,
        fundamentals: FundamentalsService,
        instruments: InstrumentDataService,
        opportunity: OpportunityService,
        *,
        macro_provider: BcbMacroProvider | None = None,
        bank_provider: BcbBankProvider | None = None,
    ) -> None:
        self.fundamentals = fundamentals
        self.instruments = instruments
        self.opportunity = opportunity
        self.macro_provider = macro_provider
        self.bank_provider = bank_provider
        self._semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)

    async def resolve(self, request: QualityFactsRequest) -> QualityFactsResponse:
        has_stocks = any(asset.kind is QualityAssetKind.stock for asset in request.assets)
        sector_task = (
            asyncio.create_task(self.fundamentals.sector_universe())
            if has_stocks and hasattr(self.fundamentals, "sector_universe")
            else None
        )
        macro_task = (
            asyncio.create_task(self.macro_provider.snapshot())
            if has_stocks and self.macro_provider is not None
            else None
        )
        assets = await asyncio.gather(
            *(self._bounded(asset, sector_task, macro_task) for asset in request.assets)
        )
        return QualityFactsResponse(assets=list(assets), refreshed_at=datetime.now(UTC))

    async def _bounded(
        self,
        asset: QualityAssetRequest,
        sector_task: asyncio.Task[dict[str, list[SectorCompany]]] | None,
        macro_task: asyncio.Task[MacroQualitySnapshot] | None,
    ) -> QualityAssetFacts:
        async with self._semaphore:
            try:
                return await self._resolve_asset(asset, sector_task, macro_task)
            except APIError as error:
                return QualityAssetFacts(
                    ticker=asset.ticker,
                    kind=asset.kind,
                    unavailable_reason=error.message,
                )

    async def _resolve_asset(
        self,
        asset: QualityAssetRequest,
        sector_task: asyncio.Task[dict[str, list[SectorCompany]]] | None,
        macro_task: asyncio.Task[MacroQualitySnapshot] | None,
    ) -> QualityAssetFacts:
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
        return await self._stock_facts(
            asset,
            instrument_data,
            opportunity,
            sector_task,
            macro_task,
        )

    async def _stock_facts(
        self,
        asset: QualityAssetRequest,
        instrument_data: InstrumentDataResponse,
        opportunity: OpportunityResponse,
        sector_task: asyncio.Task[dict[str, list[SectorCompany]]] | None,
        macro_task: asyncio.Task[MacroQualitySnapshot] | None,
    ) -> QualityAssetFacts:
        instrument = opportunity.instrument or instrument_data.instrument
        if (
            instrument is None
            or instrument.category == "INTERNATIONAL"
            or instrument.instrument_type is InstrumentType.bdr
        ):
            return await self._international_facts(asset, instrument_data, opportunity)
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
        sector_universe = await sector_task if sector_task is not None else {}
        macro = await macro_task if macro_task is not None else None
        profile = _stock_profile(
            snapshot.sector,
            snapshot.company_name or instrument.name,
        )
        peers = _sector_peers(snapshot, sector_universe, profile)
        bank = (
            await self.bank_provider.snapshot(
                snapshot.company_name or instrument.name or asset.ticker
            )
            if profile == "bank" and self.bank_provider is not None
            else None
        )
        return _financial_facts(
            asset,
            snapshot,
            instrument.isin,
            opportunity,
            peers,
            macro,
            bank,
        )

    async def _international_facts(
        self,
        asset: QualityAssetRequest,
        instrument_data: InstrumentDataResponse,
        opportunity: OpportunityResponse,
    ) -> QualityAssetFacts:
        """Quality evidence for a listing outside the CVM filing universe.

        The statements are read from the public annual timeseries and then run
        through the same derivation as a Brazilian issuer, so an international
        stock and a REIT are scored on the same definitions rather than on a
        thinner parallel set. Sector peers and prudential data have no free
        international equivalent, so those inputs stay absent and their weight
        is redistributed by the consumer.
        """
        instrument = opportunity.instrument or instrument_data.instrument
        underlying_ticker = instrument.underlying_ticker if instrument else None
        if instrument is not None and instrument.instrument_type is InstrumentType.bdr:
            if not underlying_ticker:
                return QualityAssetFacts(
                    ticker=asset.ticker,
                    kind=asset.kind,
                    canonical_id=instrument.isin,
                    unavailable_reason=(
                        instrument.underlying_unavailable_reason
                        or (
                            "BDR underlying ticker is unresolved; international fundamentals "
                            "were not queried"
                        )
                    ),
                )
            snapshot = await self.fundamentals.snapshot(
                asset.ticker,
                underlying_ticker=underlying_ticker,
                underlying_name=instrument.underlying_name,
                instrument=instrument,
            )
        else:
            snapshot = await self.fundamentals.snapshot(asset.ticker)
        if snapshot.trailing_twelve_months is None:
            return _international_stock_facts(
                asset,
                instrument_data,
                snapshot.unavailable_reason,
            )
        facts = _financial_facts(
            asset,
            snapshot,
            instrument_data.instrument.isin if instrument_data.instrument else None,
            opportunity,
            [],
            None,
            None,
        )
        # The derivations are shared with the CVM path, which labels every fact
        # it produces with its own source. Restate the origin so the provenance
        # names the statements these figures actually came from.
        origin = snapshot.periods[-1].source if snapshot.periods else SOURCE_STATEMENTS
        isin = instrument_data.instrument.isin if instrument_data.instrument else None
        return facts.model_copy(
            update={
                # A foreign listing is identified by its ISIN; it has no CNPJ.
                "canonical_id": isin or facts.canonical_id,
                "facts": [
                    fact.model_copy(update={"source": origin})
                    if fact.source == _CVM_SOURCE
                    else fact
                    for fact in facts.facts
                ],
                "sources": [origin],
                "warnings": [
                    *facts.warnings,
                    "International peers and prudential evidence are unavailable",
                ],
            }
        )


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
    opportunity: OpportunityResponse,
    peers: list[SectorCompany],
    macro: MacroQualitySnapshot | None,
    bank: BankQualitySnapshot | None,
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
    profile = _stock_profile(snapshot.sector, snapshot.company_name)
    recent = annual[-5:]
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
        _growth_fact("share_dilution", recent, lambda item: item.shares_outstanding),
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
        _median_fact(
            "roe_5y_median",
            recent,
            lambda item: _safe_ratio(item.net_income, item.equity),
        ),
        _threshold_frequency(
            "roe_above_15_frequency",
            recent,
            lambda item: _safe_ratio(item.net_income, item.equity),
            Decimal("0.15"),
        ),
        _median_fact(
            "cash_conversion_median",
            recent,
            lambda item: _safe_ratio(item.operating_cash_flow, item.net_income),
        ),
        _stability_fact(
            "gross_margin_volatility",
            recent,
            lambda item: _safe_ratio(item.gross_profit, item.revenue),
        ),
        _stability_fact(
            "operating_margin_volatility",
            recent,
            lambda item: _safe_ratio(item.ebit, item.revenue),
        ),
        *_macro_facts(recent, macro),
        *_peer_facts(period, peers),
        *_market_scale_facts(opportunity),
        *_dividend_facts(annual, opportunity.fund_distributions),
        *_bank_facts(bank),
    ]
    facts, warnings = _validated_financial_facts(facts)
    if profile == "bank" and (bank is None or bank.basel_ratio is None):
        warnings.append("BCB IFData did not resolve current prudential capital evidence")
    sources = sorted(
        {item.selected_source for item in snapshot.provenance if item.selected_source is not None}
        | {source for fact in facts for source in (fact.source or "").split(",") if source}
        | {_CVM_SOURCE}
    )
    return QualityAssetFacts(
        ticker=request.ticker,
        kind=request.kind,
        canonical_id=snapshot.cnpj or isin,
        profile=profile,
        facts=facts,
        sources=sources,
        warnings=warnings,
    )


def _international_stock_facts(
    request: QualityAssetRequest,
    data: InstrumentDataResponse,
    unavailable_reason: str | None = None,
) -> QualityAssetFacts:
    fundamentals = data.fundamentals
    if (
        unavailable_reason
        and data.instrument is not None
        and data.instrument.instrument_type is InstrumentType.bdr
    ):
        return QualityAssetFacts(
            ticker=request.ticker,
            kind=request.kind,
            canonical_id=data.instrument.isin,
            unavailable_reason=unavailable_reason,
        )
    if fundamentals is None:
        return QualityAssetFacts(
            ticker=request.ticker,
            kind=request.kind,
            canonical_id=data.instrument.isin if data.instrument else None,
            unavailable_reason=(
                unavailable_reason or "No public international fundamentals were resolved"
            ),
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
        profile=_etf_profile(profile.description, profile.asset_types, profile.sectors),
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
    latest = reports[-1] if reports else None
    profile = (
        request.profile
        if request.profile and request.profile != "indeterminado"
        else _fund_profile(latest, opportunity.instrument)
    )
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
        _value_fact(
            "net_assets",
            latest.net_assets if latest else None,
            "currency",
            latest.as_of if latest else None,
            source,
        ),
        _value_fact(
            "shareholder_count",
            latest.shareholder_count if latest else None,
            "count",
            latest.as_of if latest else None,
            source,
        ),
        _value_fact(
            "fund_age_years",
            _age_years(latest.inception_date, latest.as_of)
            if latest and latest.inception_date
            else None,
            "years",
            latest.as_of if latest else None,
            source,
        ),
        _value_fact(
            "administration_fee_ratio",
            latest.administration_fee_ratio if latest else None,
            "ratio",
            latest.as_of if latest else None,
            source,
        ),
        _administrator_stability(reports),
        _metric_fact(
            "daily_traded_value",
            opportunity.metrics.average_daily_traded_value,
            "currency",
        ),
        _ratio(
            "liability_ratio",
            latest.total_liabilities if latest else None,
            latest.total_assets if latest else None,
            latest.as_of if latest else None,
        ),
        _ratio(
            "property_allocation",
            latest.property_assets if latest else None,
            latest.total_assets if latest else None,
            latest.as_of if latest else None,
        ),
        _ratio(
            "credit_allocation",
            latest.credit_assets if latest else None,
            latest.total_assets if latest else None,
            latest.as_of if latest else None,
        ),
        _ratio(
            "liquid_asset_ratio",
            latest.liquid_assets if latest else None,
            latest.total_assets if latest else None,
            latest.as_of if latest else None,
        ),
        _issuance_nav_preservation(reports),
        _shareholder_growth(reports),
        _nav_total_consistency(latest),
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
        profile=profile,
        facts=facts,
        sources=(
            [source, "fundamentus"]
            if opportunity.metrics.average_daily_traded_value
            and opportunity.metrics.average_daily_traded_value.value is not None
            else [source]
        )
        if reports or distributions
        else [],
        warnings=warnings,
        unavailable_reason=None
        if reports or distributions
        else "No public fund history was resolved",
    )


def _etf_profile(
    description: str | None,
    asset_types: Iterable[object],
    sectors: Iterable[object],
) -> str:
    labels = " ".join(
        [
            description or "",
            *(str(getattr(item, "name", "")) for item in asset_types),
        ]
    )
    folded = _fold(labels)
    if any(token in folded for token in ("BITCOIN", "CRYPTO", "DIGITAL ASSET")):
        return "crypto"
    if any(token in folded for token in ("BOND", "FIXED INCOME", "TREASURY", "CREDIT")):
        return "fixed_income"
    sector_weights = [getattr(item, "weight", Decimal("0")) for item in sectors]
    scale = Decimal("100") if sector_weights and max(sector_weights) > 1 else Decimal("1")
    if sector_weights and max(sector_weights) / scale >= Decimal("0.65"):
        return "thematic"
    return "broad"


def _fund_profile(
    latest: FundMonthlyReport | None,
    instrument: object,
) -> str:
    instrument_type = getattr(instrument, "instrument_type", None)
    if instrument_type is InstrumentType.fi_infra:
        return "fi_infra"
    property_ratio = (
        _safe_ratio(latest.property_assets, latest.total_assets) if latest is not None else None
    )
    credit_ratio = (
        _safe_ratio(latest.credit_assets, latest.total_assets) if latest is not None else None
    )
    if instrument_type is InstrumentType.fiagro:
        return "fiagro_credit" if (credit_ratio or 0) >= (property_ratio or 0) else "fiagro_land"
    if property_ratio is not None and property_ratio >= Decimal("0.50"):
        return "brick"
    if credit_ratio is not None and credit_ratio >= Decimal("0.50"):
        return "paper"
    return "hybrid"


def _administrator_stability(reports: list[FundMonthlyReport]) -> QualityFact:
    recent = [item for item in reports[-60:] if item.administrator]
    if len(recent) < 12:
        return _missing_fact(
            "administrator_stability",
            "ratio",
            "At least twelve administrator observations are required",
        )
    changes = sum(
        1
        for previous, current in zip(recent, recent[1:], strict=False)
        if previous.administrator != current.administrator
    )
    value = max(Decimal("0"), Decimal("1") - Decimal(changes) * Decimal("0.50"))
    return _value_fact(
        "administrator_stability",
        value,
        "ratio",
        recent[-1].as_of,
        _CVM_SOURCE,
        confidence=_DERIVED_CONFIDENCE,
    )


def _issuance_nav_preservation(reports: list[FundMonthlyReport]) -> QualityFact:
    recent = [
        item
        for item in reports[-60:]
        if item.issued_shares is not None and item.issued_shares > 0 and item.nav_per_share > 0
    ]
    if len(recent) < 12:
        return _missing_fact(
            "issuance_nav_preservation",
            "ratio",
            "At least twelve share and NAV observations are required",
        )
    issuances = [
        (previous, current)
        for previous, current in zip(recent, recent[1:], strict=False)
        if current.issued_shares is not None
        and previous.issued_shares is not None
        and current.issued_shares > previous.issued_shares * Decimal("1.02")
    ]
    if not issuances:
        value = Decimal("1")
    else:
        preserved = sum(
            1
            for previous, current in issuances
            if current.nav_per_share >= previous.nav_per_share * Decimal("0.97")
        )
        value = Decimal(preserved) / Decimal(len(issuances))
    return _value_fact(
        "issuance_nav_preservation",
        value,
        "ratio",
        recent[-1].as_of,
        _CVM_SOURCE,
        confidence=_DERIVED_CONFIDENCE,
    )


def _shareholder_growth(reports: list[FundMonthlyReport]) -> QualityFact:
    recent = [
        item
        for item in reports[-60:]
        if item.shareholder_count is not None and item.shareholder_count > 0
    ]
    if len(recent) < 12:
        return _missing_fact(
            "shareholder_growth",
            "ratio",
            "At least twelve shareholder observations are required",
        )
    years = Decimal(str((recent[-1].as_of - recent[0].as_of).days / 365.25))
    value = _annualized_growth(
        recent[0].shareholder_count or Decimal("0"),
        recent[-1].shareholder_count or Decimal("0"),
        years,
    )
    return _value_fact(
        "shareholder_growth",
        value,
        "ratio",
        recent[-1].as_of,
        _CVM_SOURCE,
        confidence=_DERIVED_CONFIDENCE,
    )


def _nav_total_consistency(latest: FundMonthlyReport | None) -> QualityFact:
    if (
        latest is None
        or latest.net_assets is None
        or latest.issued_shares is None
        or latest.net_assets <= 0
        or latest.issued_shares <= 0
    ):
        return _missing_fact(
            "nav_total_consistency_error",
            "ratio",
            "NAV, total assets and issued shares are required",
        )
    implied = latest.nav_per_share * latest.issued_shares
    error = abs(implied - latest.net_assets) / max(implied, latest.net_assets)
    return _value_fact(
        "nav_total_consistency_error",
        error,
        "ratio",
        latest.as_of,
        _CVM_SOURCE,
        confidence=_DERIVED_CONFIDENCE,
    )


def _ratio(
    key: str,
    numerator: Decimal | None,
    denominator: Decimal | None,
    reference: date | None,
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


def _median_fact(
    key: str,
    periods: list[FinancialPeriod],
    getter: Callable[[FinancialPeriod], Decimal | None],
) -> QualityFact:
    observations = _observations(periods, getter)
    if len(observations) < 3:
        return _missing_fact(key, "ratio", "At least three annual observations are required")
    return QualityFact(
        key=key,
        value=_median([item.value for item in observations]),
        unit="ratio",
        as_of=observations[-1].as_of,
        source=_CVM_SOURCE,
        confidence=_DERIVED_CONFIDENCE,
        status="valid",
        history=observations,
    )


def _threshold_frequency(
    key: str,
    periods: list[FinancialPeriod],
    getter: Callable[[FinancialPeriod], Decimal | None],
    threshold: Decimal,
) -> QualityFact:
    observations = _observations(periods, getter)
    if len(observations) < 3:
        return _missing_fact(key, "ratio", "At least three annual observations are required")
    passing = sum(1 for item in observations if item.value >= threshold)
    return QualityFact(
        key=key,
        value=Decimal(passing) / Decimal(len(observations)),
        unit="ratio",
        as_of=observations[-1].as_of,
        source=_CVM_SOURCE,
        confidence=_DERIVED_CONFIDENCE,
        status="valid",
        history=observations,
    )


def _macro_facts(
    periods: list[FinancialPeriod],
    macro: MacroQualitySnapshot | None,
) -> list[QualityFact]:
    if macro is None:
        return [
            _missing_fact(key, "ratio", "BCB macroeconomic series are unavailable")
            for key in (
                "revenue_real_cagr",
                "earnings_real_cagr",
                "roe_vs_selic_spread",
                "stress_profitability_frequency",
                "positive_real_revenue_growth_frequency",
            )
        ]
    return [
        _real_growth_fact("revenue_real_cagr", periods, lambda item: item.revenue, macro),
        _real_growth_fact("earnings_real_cagr", periods, lambda item: item.net_income, macro),
        _roe_selic_spread(periods, macro),
        _stress_profitability(periods, macro),
        _positive_real_growth_frequency(periods, macro),
    ]


def _real_growth_fact(
    key: str,
    periods: list[FinancialPeriod],
    getter: Callable[[FinancialPeriod], Decimal | None],
    macro: MacroQualitySnapshot,
) -> QualityFact:
    observations = _observations(periods, getter)
    if len(observations) < 3:
        return _missing_fact(key, "ratio", "At least three annual observations are required")
    first, last = observations[0], observations[-1]
    inflation = Decimal("1")
    matched_years = 0
    for year in range(first.as_of.year + 1, last.as_of.year + 1):
        annual_inflation = macro.inflation_by_year.get(year)
        if annual_inflation is not None:
            inflation *= Decimal("1") + annual_inflation
            matched_years += 1
    years = Decimal(str((last.as_of - first.as_of).days / 365.25))
    value = (
        _annualized_growth(first.value, last.value / inflation, years)
        if matched_years >= 2
        else None
    )
    return QualityFact(
        key=key,
        value=value,
        unit="ratio",
        as_of=last.as_of if value is not None else None,
        source="cvm,bcb_sgs" if value is not None else None,
        confidence=Decimal("0.88") if value is not None else Decimal("0"),
        status="valid" if value is not None else "missing_data",
        unavailable_reason=(
            None
            if value is not None
            else "Comparable positive values and IPCA history are required"
        ),
        history=observations,
    )


def _roe_selic_spread(
    periods: list[FinancialPeriod],
    macro: MacroQualitySnapshot,
) -> QualityFact:
    observations = [
        QualityFactObservation(as_of=item.period_end, value=roe - selic)
        for item in periods
        if (roe := _safe_ratio(item.net_income, item.equity)) is not None
        and (selic := macro.selic_by_year.get(item.period_end.year)) is not None
    ]
    if len(observations) < 3:
        return _missing_fact(
            "roe_vs_selic_spread",
            "ratio",
            "At least three matched ROE and Selic observations are required",
        )
    return QualityFact(
        key="roe_vs_selic_spread",
        value=_median([item.value for item in observations]),
        unit="ratio",
        as_of=observations[-1].as_of,
        source="cvm,bcb_sgs",
        confidence=Decimal("0.88"),
        status="valid",
        history=observations,
    )


def _stress_profitability(
    periods: list[FinancialPeriod],
    macro: MacroQualitySnapshot,
) -> QualityFact:
    stress_years = {
        2015,
        2016,
        2020,
        *(
            year
            for year, annual_selic in macro.selic_by_year.items()
            if annual_selic >= Decimal("0.12")
        ),
    }
    observations = [
        QualityFactObservation(as_of=item.period_end, value=item.net_income)
        for item in periods
        if item.period_end.year in stress_years and item.net_income is not None
    ]
    if len(observations) < 2:
        return _missing_fact(
            "stress_profitability_frequency",
            "ratio",
            "At least two public stress-period observations are required",
        )
    positives = sum(1 for item in observations if item.value > 0)
    return QualityFact(
        key="stress_profitability_frequency",
        value=Decimal(positives) / Decimal(len(observations)),
        unit="ratio",
        as_of=observations[-1].as_of,
        source="cvm,bcb_sgs",
        confidence=Decimal("0.88"),
        status="valid",
        history=observations,
    )


def _positive_real_growth_frequency(
    periods: list[FinancialPeriod],
    macro: MacroQualitySnapshot,
) -> QualityFact:
    comparable = []
    for previous, current in zip(periods, periods[1:], strict=False):
        inflation = macro.inflation_by_year.get(current.period_end.year)
        if (
            previous.revenue is None
            or previous.revenue <= 0
            or current.revenue is None
            or inflation is None
        ):
            continue
        real_growth = current.revenue / previous.revenue / (Decimal("1") + inflation) - Decimal("1")
        comparable.append(QualityFactObservation(as_of=current.period_end, value=real_growth))
    if len(comparable) < 3:
        return _missing_fact(
            "positive_real_revenue_growth_frequency",
            "ratio",
            "At least three matched revenue and IPCA intervals are required",
        )
    positives = sum(1 for item in comparable if item.value > 0)
    return QualityFact(
        key="positive_real_revenue_growth_frequency",
        value=Decimal(positives) / Decimal(len(comparable)),
        unit="ratio",
        as_of=comparable[-1].as_of,
        source="cvm,bcb_sgs",
        confidence=Decimal("0.88"),
        status="valid",
        history=comparable,
    )


def _peer_facts(
    period: FinancialPeriod,
    peers: list[SectorCompany],
) -> list[QualityFact]:
    reference = period.period_end
    comparisons = (
        (
            "roe_vs_sector_median",
            _safe_ratio(period.net_income, period.equity),
            [_safe_ratio(item.period.net_income, item.period.equity) for item in peers],
        ),
        (
            "roa_vs_sector_median",
            _safe_ratio(period.net_income, period.total_assets),
            [_safe_ratio(item.period.net_income, item.period.total_assets) for item in peers],
        ),
        (
            "net_margin_vs_sector_median",
            _safe_ratio(period.net_income, period.revenue),
            [_safe_ratio(item.period.net_income, item.period.revenue) for item in peers],
        ),
        (
            "equity_ratio_vs_sector_median",
            _safe_ratio(period.equity, period.total_assets),
            [_safe_ratio(item.period.equity, item.period.total_assets) for item in peers],
        ),
    )
    facts = [
        _peer_ratio_fact(key, value, samples, reference) for key, value, samples in comparisons
    ]
    sector_revenue = sum(
        (
            item.period.revenue
            for item in peers
            if item.period.revenue is not None and item.period.revenue > 0
        ),
        Decimal("0"),
    )
    facts.append(
        _value_fact(
            "listed_sector_revenue_share",
            _safe_ratio(period.revenue, sector_revenue),
            "ratio",
            reference,
            "cvm_sector_universe",
            confidence=Decimal("0.70"),
        )
        if len(peers) >= 3
        else _missing_fact(
            "listed_sector_revenue_share",
            "ratio",
            "At least three listed sector peers are required",
        )
    )
    return facts


def _peer_ratio_fact(
    key: str,
    value: Decimal | None,
    samples: list[Decimal | None],
    reference: date,
) -> QualityFact:
    valid = [sample for sample in samples if sample is not None and sample > 0]
    median = _median(valid) if len(valid) >= 3 else None
    return (
        _value_fact(
            key,
            _safe_ratio(value, median),
            "multiple",
            reference,
            "cvm_sector_universe",
            confidence=_PEER_CONFIDENCE,
        )
        if median is not None
        else _missing_fact(key, "multiple", "At least three positive sector peers are required")
    )


def _market_scale_facts(opportunity: OpportunityResponse) -> list[QualityFact]:
    metrics = opportunity.metrics
    return [
        _metric_fact("daily_traded_value", metrics.average_daily_traded_value, "currency"),
        _metric_fact("market_capitalization", metrics.market_capitalization, "currency"),
    ]


def _metric_fact(key: str, metric: object, unit: str) -> QualityFact:
    value = getattr(metric, "value", None)
    sources = getattr(metric, "sources", [])
    reference = getattr(metric, "as_of", None)
    return _value_fact(
        key,
        value if isinstance(value, Decimal) else None,
        unit,
        reference if isinstance(reference, date) else None,
        ",".join(str(source) for source in sources) or "public_market_data",
        confidence=_MARKET_CONFIDENCE,
    )


def _dividend_facts(
    periods: list[FinancialPeriod],
    distributions: list[FundDistribution],
) -> list[QualityFact]:
    valid = sorted(
        (item for item in distributions if item.value >= 0),
        key=lambda item: item.ex_date,
    )
    if not valid:
        return [
            _missing_fact(key, "ratio", "Public dividend history is unavailable")
            for key in (
                "dividend_history_years",
                "dividend_payment_frequency",
                "dividend_stability",
                "dividend_payout_sustainability",
            )
        ]
    reference = valid[-1].ex_date
    first_year = max(valid[0].ex_date.year, reference.year - 4)
    annual_dividends = {
        year: sum(
            (item.value for item in valid if item.ex_date.year == year),
            Decimal("0"),
        )
        for year in range(first_year, reference.year + 1)
    }
    positive = [value for value in annual_dividends.values() if value > 0]
    history_years = Decimal(reference.year - valid[0].ex_date.year + 1)
    payment_frequency = Decimal(len(positive)) / Decimal(len(annual_dividends))
    stability = _coefficient_of_variation(positive) if len(positive) >= 3 else None
    payout_samples = []
    periods_by_year = {item.period_end.year: item for item in periods}
    for year, dividend_per_share in annual_dividends.items():
        period = periods_by_year.get(year)
        if dividend_per_share <= 0 or period is None:
            continue
        earnings_per_share = _safe_ratio(period.net_income, period.shares_outstanding)
        payout = _safe_ratio(dividend_per_share, earnings_per_share)
        if payout is not None and payout >= 0:
            payout_samples.append(payout)
    sustainable = (
        Decimal(sum(1 for payout in payout_samples if payout <= Decimal("1.20")))
        / Decimal(len(payout_samples))
        if len(payout_samples) >= 3
        else None
    )
    source = ",".join(sorted({item.source for item in valid}))
    return [
        _value_fact(
            "dividend_history_years",
            history_years,
            "years",
            reference,
            source,
            confidence=_MARKET_CONFIDENCE,
        ),
        _value_fact(
            "dividend_payment_frequency",
            payment_frequency,
            "ratio",
            reference,
            source,
            confidence=_MARKET_CONFIDENCE,
        ),
        _value_fact(
            "dividend_stability",
            stability,
            "ratio",
            reference,
            source,
            confidence=Decimal("0.80"),
        ),
        _value_fact(
            "dividend_payout_sustainability",
            sustainable,
            "ratio",
            reference,
            "cvm,public_distributions",
            confidence=Decimal("0.80"),
        ),
    ]


def _bank_facts(bank: BankQualitySnapshot | None) -> list[QualityFact]:
    if bank is None:
        return [
            _missing_fact(key, "ratio", "BCB IFData evidence is unavailable")
            for key in (
                "basel_ratio",
                "core_capital_ratio",
                "regulatory_leverage_ratio",
                "high_risk_credit_ratio",
            )
        ]
    return [
        _value_fact(
            "basel_ratio",
            bank.basel_ratio,
            "ratio",
            bank.capital_as_of,
            "bcb_ifdata",
            confidence=_BCB_CONFIDENCE,
        ),
        _value_fact(
            "core_capital_ratio",
            bank.core_capital_ratio,
            "ratio",
            bank.capital_as_of,
            "bcb_ifdata",
            confidence=_BCB_CONFIDENCE,
        ),
        _value_fact(
            "regulatory_leverage_ratio",
            bank.leverage_ratio,
            "ratio",
            bank.capital_as_of,
            "bcb_ifdata",
            confidence=_BCB_CONFIDENCE,
        ),
        _value_fact(
            "high_risk_credit_ratio",
            bank.high_risk_credit_ratio,
            "ratio",
            bank.credit_as_of,
            "bcb_ifdata",
            confidence=Decimal("0.78"),
        ),
    ]


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
    "roe_5y_median": (Decimal("-2"), Decimal("3")),
    "cash_conversion_median": (Decimal("-5"), Decimal("8")),
    "revenue_real_cagr": (Decimal("-0.8"), Decimal("2")),
    "earnings_real_cagr": (Decimal("-0.8"), Decimal("2")),
    "roe_vs_selic_spread": (Decimal("-2"), Decimal("3")),
    "roe_vs_sector_median": (Decimal("-10"), Decimal("20")),
    "roa_vs_sector_median": (Decimal("-10"), Decimal("20")),
    "net_margin_vs_sector_median": (Decimal("-10"), Decimal("20")),
    "equity_ratio_vs_sector_median": (Decimal("-10"), Decimal("20")),
    "listed_sector_revenue_share": (Decimal("0"), Decimal("1")),
    "basel_ratio": (Decimal("0"), Decimal("1")),
    "core_capital_ratio": (Decimal("0"), Decimal("1")),
    "regulatory_leverage_ratio": (Decimal("0"), Decimal("1")),
    "high_risk_credit_ratio": (Decimal("0"), Decimal("1")),
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
    observations = _nav_return_observations(reports[-36:])
    if len(observations) < 6:
        return _missing_fact(
            "nav_return_volatility",
            "ratio",
            "At least six monthly NAV returns are required",
        )
    values = [item.value for item in observations]
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
    observations = _nav_return_observations(reports[-36:])
    if len(observations) < 6:
        return _missing_fact(
            "positive_nav_return_frequency",
            "ratio",
            "At least six monthly NAV returns are required",
        )
    positive = sum(1 for item in observations if item.value >= 0)
    return _value_fact(
        "positive_nav_return_frequency",
        Decimal(positive) / Decimal(len(observations)),
        "ratio",
        observations[-1].as_of,
        _CVM_SOURCE,
        confidence=_DERIVED_CONFIDENCE,
    )


def _nav_return_observations(
    reports: list[FundMonthlyReport],
) -> list[QualityFactObservation]:
    observations = []
    for index, current in enumerate(reports):
        value = current.monthly_nav_return
        if (
            value is None
            and index > 0
            and reports[index - 1].nav_per_share > 0
            and current.nav_per_share > 0
        ):
            value = current.nav_per_share / reports[index - 1].nav_per_share - Decimal("1")
        if value is not None:
            observations.append(QualityFactObservation(as_of=current.as_of, value=value))
    return observations


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


def _median(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal("2")


def _coefficient_of_variation(values: list[Decimal]) -> Decimal | None:
    if len(values) < 2:
        return None
    average = sum(values, Decimal("0")) / Decimal(len(values))
    if average <= 0:
        return None
    variance = sum(((value - average) ** 2 for value in values), Decimal("0")) / Decimal(
        len(values)
    )
    return Decimal(str(float(variance) ** 0.5)) / average


def _sector_peers(
    snapshot: FundamentalsSnapshot,
    universe: dict[str, list[SectorCompany]],
    profile: str,
) -> list[SectorCompany]:
    if profile not in {"bank", "insurer"}:
        return universe.get(snapshot.sector or "", [])
    return [
        company
        for companies in universe.values()
        for company in companies
        if _stock_profile(company.sector, company.company_name) == profile
    ]


def _stock_profile(sector: str | None, company_name: str | None = None) -> str:
    folded = _fold(f"{sector or ''} {company_name or ''}")
    if any(token in folded for token in ("BANCO", "BCO ", "INTERMEDIAR", "SERVICOS FINANCEIROS")):
        return "bank"
    if any(token in folded for token in ("SEGURO", "SEGURIDADE", "PREVIDENCIA")):
        return "insurer"
    if any(
        token in folded
        for token in (
            "PETROLEO",
            "MINERACAO",
            "SIDERURGIA",
            "METALURGIA",
            "PAPEL E CELULOSE",
        )
    ):
        return "commodity"
    if any(token in folded for token in ("ENERGIA ELETRICA", "SANEAMENTO", "GAS")):
        return "utility"
    return "industrial"


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return " ".join(normalized.encode("ascii", "ignore").decode("ascii").upper().split())


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
