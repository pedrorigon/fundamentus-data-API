from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

MAX_FUNDAMENTAL_TICKERS = 20


class FieldProvenance(BaseModel):
    """Origin and reliability of a single resolved field."""

    field_name: str
    value: Decimal | None = None
    selected_source: str | None = None
    fallbacks_attempted: list[str] = Field(default_factory=list)
    reference_date: date | None = None
    retrieved_at: date | None = None
    confidence: Decimal = Decimal("0")
    status: str = "missing_data"
    divergences: dict[str, Decimal] = Field(default_factory=dict)


class FinancialPeriod(BaseModel):
    """Derived financial figures for one reported period."""

    period_end: date
    published_at: date | None = None
    consolidated: bool
    annual: bool
    revenue: Decimal | None = None
    gross_profit: Decimal | None = None
    ebit: Decimal | None = None
    ebitda: Decimal | None = None
    financial_result: Decimal | None = None
    net_income: Decimal | None = None
    equity: Decimal | None = None
    total_assets: Decimal | None = None
    current_assets: Decimal | None = None
    current_liabilities: Decimal | None = None
    cash_and_equivalents: Decimal | None = None
    short_term_debt: Decimal | None = None
    long_term_debt: Decimal | None = None
    gross_debt: Decimal | None = None
    net_debt: Decimal | None = None
    operating_cash_flow: Decimal | None = None
    capex: Decimal | None = None
    free_cash_flow: Decimal | None = None
    depreciation: Decimal | None = None
    shares_outstanding: Decimal | None = None
    source: str = "cvm"


class FundamentalsSnapshot(BaseModel):
    """Full fundamentals history for one ticker."""

    ticker: str
    cnpj: str | None = None
    company_name: str | None = None
    sector: str | None = None
    currency: str = "BRL"
    periods: list[FinancialPeriod] = Field(default_factory=list)
    trailing_twelve_months: FinancialPeriod | None = None
    shares_outstanding: Decimal | None = None
    earnings_per_share: Decimal | None = None
    book_value_per_share: Decimal | None = None
    recurring_dividends_per_share: Decimal | None = None
    provenance: list[FieldProvenance] = Field(default_factory=list)
    unavailable_reason: str | None = None


class FundamentalsResponse(BaseModel):
    ticker: str
    snapshot: FundamentalsSnapshot | None = None
    refreshed_at: date | None = None


class FundamentalsBatchRequest(BaseModel):
    """Tickers to resolve in one round, bounded like the other batch routes."""

    tickers: list[str] = Field(min_length=1, max_length=MAX_FUNDAMENTAL_TICKERS)

    @field_validator("tickers")
    @classmethod
    def normalize(cls, values: list[str]) -> list[str]:
        normalized = [value.strip().upper() for value in values if value and value.strip()]
        if not normalized:
            raise ValueError("At least one ticker is required")
        if len(set(normalized)) != len(normalized):
            raise ValueError("Tickers must not contain duplicates")
        return normalized


class FundamentalsBatchResponse(BaseModel):
    assets: list[FundamentalsResponse] = Field(default_factory=list)
    refreshed_at: date | None = None


class SectorCompany(BaseModel):
    """One filing company placed in its sector, for peer aggregation."""

    cnpj: str
    company_name: str
    sector: str
    period: FinancialPeriod


class SectorUniverseResponse(BaseModel):
    """Every filing company grouped by sector."""

    sectors: dict[str, list[SectorCompany]] = Field(default_factory=dict)
    refreshed_at: date | None = None


class PeerGroup(BaseModel):
    """Sector aggregates used for relative valuation."""

    sector: str
    tickers: list[str] = Field(default_factory=list)
    sample_size: int = 0
    median_price_to_earnings: Decimal | None = None
    median_price_to_book: Decimal | None = None
    median_ev_to_ebitda: Decimal | None = None
    median_ev_to_ebit: Decimal | None = None
    median_price_to_free_cash_flow: Decimal | None = None
    median_earnings_yield: Decimal | None = None
    median_free_cash_flow_yield: Decimal | None = None
    median_dividend_yield: Decimal | None = None
    unavailable_reason: str | None = None
