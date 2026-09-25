import re
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

_TICKER_RE = re.compile(r"^[A-Z0-9]+(?:[.\-][A-Z0-9]+)*$")

JsonValue = str | Decimal | date | int | bool | None


class APIModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True)


class FieldData(APIModel):
    label: str
    key_normalized: str
    value: JsonValue
    raw_value: str | None
    value_type: str = Field(description="null, date, percent, number, money or text.")


class DetailSection(APIModel):
    name: str
    key_normalized: str
    fields: list[FieldData] = Field(default_factory=list)


class AssetDetails(APIModel):
    ticker: str
    company_name: str | None = None
    asset_type: str | None = None
    quote: Decimal | None = None
    quote_date: date | None = None
    market_value: Decimal | None = None
    enterprise_value: Decimal | None = None
    shares_count: Decimal | None = None
    last_balance_date: date | None = None
    sector: str | None = None
    subsector: str | None = None
    average_daily_volume_2m: Decimal | None = None
    book_value_per_share: Decimal | None = None
    earnings_per_share: Decimal | None = None
    min_52_weeks: Decimal | None = None
    max_52_weeks: Decimal | None = None
    sections: list[DetailSection] = Field(default_factory=list)
    source_url: str
    scraped_at: datetime


class DividendPeriod(StrEnum):
    all = "all"
    past = "past"
    future = "future"
    upcoming_ex_date = "upcoming_ex_date"


class Dividend(APIModel):
    ex_date: date | None
    payment_date: date | None
    value: Decimal | None
    type: str | None
    shares_ratio: Decimal | None = None
    is_future_payment: bool
    is_future_ex_date: bool
    raw: dict[str, str | None]


class AssetResponse(APIModel):
    ticker: str
    details: AssetDetails | None = None
    dividends: list[Dividend] | None = None
    cached: dict[str, bool] = Field(default_factory=dict)


class InstrumentType(StrEnum):
    stock = "stock"
    unit = "unit"
    bdr = "bdr"
    fii = "fii"
    fi_infra = "fi_infra"
    fiagro = "fiagro"
    etf = "etf"
    fund = "fund"
    unknown = "unknown"


class InstrumentMetadata(APIModel):
    ticker: str
    name: str | None = None
    instrument_type: InstrumentType
    category: str | None = None
    cfi_code: str | None = None
    isin: str | None = None
    identifiers: dict[str, str] = Field(default_factory=dict)
    currency: str | None = None
    exchange: str | None = None
    country: str | None = None
    underlying_ticker: str | None = None
    underlying_name: str | None = None
    underlying_exchange: str | None = None
    underlying_country: str | None = None
    underlying_identifiers: dict[str, str] = Field(default_factory=dict)
    underlying_source: str | None = None
    underlying_unavailable_reason: str | None = None
    reference_date: date | None = None
    source: str = "b3"
    confidence: str = "high"


class InstrumentBatchRequest(APIModel):
    tickers: list[str] = Field(min_length=1, max_length=100)

    @field_validator("tickers")
    @classmethod
    def normalize_tickers(cls, values: list[str]) -> list[str]:
        normalized = [value.strip().upper() for value in values]
        if any(len(value) > 12 or _TICKER_RE.fullmatch(value) is None for value in normalized):
            raise ValueError("invalid ticker")
        return list(dict.fromkeys(normalized))


class InstrumentBatchResponse(APIModel):
    instruments: list[InstrumentMetadata] = Field(default_factory=list)


class FundAllocation(APIModel):
    name: str
    weight: Decimal


class FundHolding(APIModel):
    symbol: str
    description: str | None = None
    weight: Decimal


class FundProfile(APIModel):
    net_assets: Decimal | None = None
    net_assets_date: date | None = None
    net_assets_source: str | None = None
    net_expense_ratio: Decimal | None = None
    portfolio_turnover: Decimal | None = None
    dividend_yield: Decimal | None = None
    nav: Decimal | None = None
    inception_date: date | None = None
    description: str | None = None
    sectors: list[FundAllocation] = Field(default_factory=list)
    asset_types: list[FundAllocation] = Field(default_factory=list)
    holdings: list[FundHolding] = Field(default_factory=list)
    source: str


class InternationalFundamentals(APIModel):
    description: str | None = None
    country: str | None = None
    sector: str | None = None
    industry: str | None = None
    exchange: str | None = None
    currency: str | None = None
    market_capitalization: Decimal | None = None
    price_to_earnings: Decimal | None = None
    price_to_book: Decimal | None = None
    earnings_per_share: Decimal | None = None
    dividend_yield: Decimal | None = None
    source: str


class MarketQuote(APIModel):
    price: Decimal
    currency: str
    exchange: str | None = None
    quoted_at: datetime | None = None
    source: str


class OpportunityObservation(APIModel):
    """One normalized source value retained for later reconciliation."""

    value: Decimal
    source: str
    as_of: date | None = None
    unit: str | None = None
    source_lineage: list[str] = Field(default_factory=list)
    independent_origin: str | None = None

    @field_validator("value")
    @classmethod
    def finite_value(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("observation value must be finite")
        return value


class InstrumentDataResponse(APIModel):
    ticker: str
    instrument: InstrumentMetadata | None = None
    quote: MarketQuote | None = None
    fund_profile: FundProfile | None = None
    fundamentals: InternationalFundamentals | None = None
    unavailable_reason: str | None = None
    refreshed_at: datetime


class InstrumentSearchResponse(APIModel):
    query: str
    results: list[InstrumentMetadata] = Field(default_factory=list)
    limited: bool = True
    unavailable_reason: str | None = None


class OpportunityMetric(APIModel):
    value: Decimal | None = None
    as_of: date | None = None
    unit: str | None = None
    sources: list[str] = Field(default_factory=list)
    independent_sources: list[str] = Field(default_factory=list)
    source_lineage: list[str] = Field(default_factory=list)
    observations: list[OpportunityObservation] = Field(default_factory=list)
    # Values excluded from the selected vote remain visible to explain
    # outliers, unit mismatches, and implausible provider responses.
    rejected_observations: list[OpportunityObservation] = Field(default_factory=list)
    consensus_status: str = "missing_data"
    confidence: Decimal = Decimal("0")
    unavailable_reason: str | None = None

    @field_validator("value", "confidence")
    @classmethod
    def finite_numbers(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and not value.is_finite():
            raise ValueError("metric values must be finite")
        return value

    @field_validator("confidence")
    @classmethod
    def confidence_range(cls, value: Decimal) -> Decimal:
        if value < 0 or value > 1:
            raise ValueError("metric confidence must be between zero and one")
        return value


class OpportunityMetrics(APIModel):
    current_price: OpportunityMetric
    shares_outstanding: OpportunityMetric
    earnings_per_share: OpportunityMetric
    book_value_per_share: OpportunityMetric
    price_to_book: OpportunityMetric
    price_to_earnings: OpportunityMetric
    dividend_yield_12m: OpportunityMetric
    dividends_12m: OpportunityMetric
    graham_price: OpportunityMetric
    bazin_price: OpportunityMetric
    min_52_weeks: OpportunityMetric
    max_52_weeks: OpportunityMetric
    latest_distribution: OpportunityMetric | None = None
    median_distribution_3m: OpportunityMetric | None = None
    median_distribution_6m: OpportunityMetric | None = None
    average_daily_traded_value: OpportunityMetric | None = None
    market_capitalization: OpportunityMetric | None = None


class FundMonthlyReport(APIModel):
    as_of: date
    nav_per_share: Decimal
    monthly_distribution_yield: Decimal | None = None
    monthly_nav_return: Decimal | None = None
    monthly_effective_return: Decimal | None = None
    net_assets: Decimal | None = None
    issued_shares: Decimal | None = None
    shareholder_count: Decimal | None = None
    administration_fee_ratio: Decimal | None = None
    total_assets: Decimal | None = None
    total_liabilities: Decimal | None = None
    property_assets: Decimal | None = None
    credit_assets: Decimal | None = None
    liquid_assets: Decimal | None = None
    inception_date: date | None = None
    segment: str | None = None
    administrator: str | None = None
    source: str = "cvm"


class FundReportSeries(APIModel):
    cnpj: str | None = None
    reports: list[FundMonthlyReport] = Field(default_factory=list)


class FundDistribution(APIModel):
    ex_date: date
    value: Decimal
    source: str


class FundMonthlyDistribution(APIModel):
    """Manager-reported income for a reference month, including explicit zeroes."""

    reference_month: date
    payment_date: date
    value: Decimal
    report_as_of: date
    source: str
    published_at: datetime | None = None


class FundDistributionEvidence(APIModel):
    """Auditable reconciliation result for one distribution date.

    ``FundDistribution`` remains the compact, backwards-compatible projection
    consumed by existing clients.  This companion record retains every source
    observation, including unresolved conflicts that cannot safely be reduced
    to one numeric value.
    """

    ex_date: date
    value: Decimal | None = None
    status: str = "missing_data"
    reason: str
    confidence: Decimal = Decimal("0")
    sources: list[str] = Field(default_factory=list)
    independent_sources: list[str] = Field(default_factory=list)
    source_lineage: list[str] = Field(default_factory=list)
    observations: list[OpportunityObservation] = Field(default_factory=list)
    rejected_observations: list[OpportunityObservation] = Field(default_factory=list)

    @field_validator("value", "confidence")
    @classmethod
    def finite_numbers(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and not value.is_finite():
            raise ValueError("distribution evidence values must be finite")
        return value

    @field_validator("confidence")
    @classmethod
    def confidence_range(cls, value: Decimal) -> Decimal:
        if value < 0 or value > 1:
            raise ValueError("distribution evidence confidence must be between zero and one")
        return value


class OpportunityResponse(APIModel):
    ticker: str
    instrument: InstrumentMetadata | None = None
    metrics: OpportunityMetrics
    fund_reports: FundReportSeries | None = None
    fund_distributions: list[FundDistribution] = Field(default_factory=list)
    fund_monthly_distributions: list[FundMonthlyDistribution] = Field(default_factory=list)
    fund_distribution_evidence: list[FundDistributionEvidence] = Field(default_factory=list)
    # Stable provider error codes are retained so callers can distinguish a
    # legitimate missing observation from a temporary source outage without
    # exposing upstream response bodies or exception text.
    source_failures: dict[str, str] = Field(default_factory=dict)
    refreshed_at: datetime


class BatchAssetResponse(APIModel):
    count: int
    results: list[AssetResponse]


class CacheInvalidationRequest(APIModel):
    ticker: str | None = None
    token: str | None = None


class CacheInvalidationResponse(APIModel):
    invalidated: bool
    ticker: str | None = None


class HealthResponse(APIModel):
    status: str
    version: str
    environment: str
    checks: dict[str, Any] = Field(default_factory=dict)
