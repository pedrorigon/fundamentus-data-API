from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

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
    currency: str | None = None
    reference_date: date | None = None
    source: str = "b3"
    confidence: str = "high"


class OpportunityMetric(APIModel):
    value: Decimal | None = None
    as_of: date | None = None
    sources: list[str] = Field(default_factory=list)
    unavailable_reason: str | None = None


class OpportunityMetrics(APIModel):
    current_price: OpportunityMetric
    price_to_book: OpportunityMetric
    price_to_earnings: OpportunityMetric
    dividend_yield_12m: OpportunityMetric
    dividends_12m: OpportunityMetric
    graham_price: OpportunityMetric
    bazin_price: OpportunityMetric
    min_52_weeks: OpportunityMetric
    max_52_weeks: OpportunityMetric


class OpportunityResponse(APIModel):
    ticker: str
    instrument: InstrumentMetadata | None = None
    metrics: OpportunityMetrics
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
