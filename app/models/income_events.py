from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field, field_validator

from app.models.assets import APIModel
from app.parsers.normalizers import normalize_ticker


class IncomeEventStatus(StrEnum):
    tentative = "tentative"
    corroborated = "corroborated"
    verified = "verified"
    conflicted = "conflicted"
    corrected = "corrected"
    cancelled = "cancelled"


class IncomeFieldConfidence(StrEnum):
    tentative = "tentative"
    corroborated = "corroborated"
    authoritative = "authoritative"


class IncomeInstrumentRequest(APIModel):
    ticker: str
    isin: str | None = None
    name: str | None = None

    @field_validator("ticker")
    @classmethod
    def normalize_requested_ticker(cls, value: str) -> str:
        return normalize_ticker(value)

    @field_validator("isin")
    @classmethod
    def normalize_requested_isin(cls, value: str | None) -> str | None:
        normalized = (value or "").strip().upper()
        return normalized or None


class IncomeEventObservation(APIModel):
    source: str
    lineage: str
    source_event_id: str
    ticker: str
    isin: str | None = None
    event_type: str
    ex_date: date | None = None
    payment_date: date | None = None
    unit_price: Decimal | None = None
    reference_period: str | None = None
    source_status: str = "active"
    source_version: int = 1
    authority: int = Field(ge=0, le=100)
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload_hash: str


class CanonicalIncomeEvent(APIModel):
    event_id: str
    ticker: str
    isin: str | None = None
    event_type: str
    ex_date: date
    payment_date: date
    unit_price: Decimal
    reference_period: str | None = None
    status: IncomeEventStatus
    revision: int = 1
    sources: list[str] = Field(default_factory=list)
    field_sources: dict[str, str] = Field(default_factory=dict)
    field_confidence: dict[str, IncomeFieldConfidence] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def projectable(self) -> bool:
        return self.status in {IncomeEventStatus.corroborated, IncomeEventStatus.verified}


class IncomeSourceCoverage(APIModel):
    source: str
    ticker: str
    status: str
    complete: bool
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    detail: str | None = None


class IncomeEventRefreshRequest(APIModel):
    instruments: list[IncomeInstrumentRequest] = Field(min_length=1, max_length=100)
    as_of: date | None = None


class IncomeEventRefreshResponse(APIModel):
    requested: int
    observations: int
    published: int
    failed_sources: list[str] = Field(default_factory=list)
    cursor: int


class IncomeEventBatchRequest(APIModel):
    tickers: list[str] = Field(min_length=1, max_length=100)
    from_date: date | None = None
    to_date: date | None = None
    include_tentative: bool = False

    @field_validator("tickers")
    @classmethod
    def normalize_requested_tickers(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(normalize_ticker(value) for value in values))


class IncomeEventBatchResponse(APIModel):
    events: list[CanonicalIncomeEvent] = Field(default_factory=list)
    cursor: int
    refreshed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class IncomeEventChangesResponse(APIModel):
    events: list[CanonicalIncomeEvent] = Field(default_factory=list)
    cursor: int
    has_more: bool = False


__all__ = [
    "CanonicalIncomeEvent",
    "IncomeEventBatchRequest",
    "IncomeEventBatchResponse",
    "IncomeEventChangesResponse",
    "IncomeEventObservation",
    "IncomeEventRefreshRequest",
    "IncomeEventRefreshResponse",
    "IncomeEventStatus",
    "IncomeFieldConfidence",
    "IncomeInstrumentRequest",
    "IncomeSourceCoverage",
]
