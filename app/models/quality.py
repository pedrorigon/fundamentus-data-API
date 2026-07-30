from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

MAX_QUALITY_ASSETS = 20


class QualityAssetKind(StrEnum):
    stock = "stock"
    real_estate_fund = "real_estate_fund"
    etf = "etf"
    crypto = "crypto"
    fixed_income = "fixed_income"


class QualityFactStatus(StrEnum):
    valid = "valid"
    missing_data = "missing_data"
    not_applicable = "not_applicable"


class QualityAssetRequest(BaseModel):
    ticker: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9._-]+$")
    kind: QualityAssetKind
    profile: str | None = Field(default=None, min_length=1, max_length=40, pattern=r"^[a-z_]+$")

    @field_validator("ticker", mode="before")
    @classmethod
    def normalize_ticker(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value


class QualityFactsRequest(BaseModel):
    assets: list[QualityAssetRequest] = Field(min_length=1, max_length=MAX_QUALITY_ASSETS)

    @field_validator("assets")
    @classmethod
    def reject_duplicates(cls, value: list[QualityAssetRequest]) -> list[QualityAssetRequest]:
        identities = [(asset.ticker, asset.kind) for asset in value]
        if len(identities) != len(set(identities)):
            raise ValueError("assets must not contain duplicates")
        return value


class QualityFactObservation(BaseModel):
    as_of: date
    value: Decimal


class QualityFact(BaseModel):
    key: str
    value: Decimal | None = None
    unit: str
    as_of: date | None = None
    source: str | None = None
    confidence: Decimal = Decimal("0")
    status: QualityFactStatus = QualityFactStatus.missing_data
    unavailable_reason: str | None = None
    history: list[QualityFactObservation] = Field(default_factory=list)


class QualityAssetFacts(BaseModel):
    ticker: str
    kind: QualityAssetKind
    canonical_id: str | None = None
    profile: str | None = None
    facts: list[QualityFact] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    unavailable_reason: str | None = None


class QualityFactsResponse(BaseModel):
    assets: list[QualityAssetFacts]
    refreshed_at: datetime
