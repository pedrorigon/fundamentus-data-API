from __future__ import annotations

import unicodedata
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator

from app.models.assets import OpportunityResponse
from app.models.fundamentals import FundamentalsSnapshot
from app.models.quality import QualityAssetFacts, QualityAssetKind

ASSESSMENT_TIMEZONE = ZoneInfo("America/Sao_Paulo")
ASSESSMENT_HOURS = frozenset({12, 14, 16, 19})
MAX_PROFILE_LENGTH = 40
MAX_VENUE_LENGTH = 24
MAX_CORPORATE_NAME_LENGTH = 240


class AssessmentRunStatus(StrEnum):
    completed = "completed"
    processing = "processing"
    failed = "failed"


class AssessmentComponentStatus(StrEnum):
    available = "available"
    missing_data = "missing_data"
    unsupported = "unsupported"
    failed = "failed"


class AssessmentComponentState(BaseModel):
    status: AssessmentComponentStatus
    error_code: str | None = None
    error: str | None = None
    retryable: bool = False


class AssessmentSnapshotRequest(BaseModel):
    """One bounded, globally shareable assessment period request.

    The period is a scheduler slot rather than an arbitrary timestamp.  Keeping
    that invariant in the public model prevents callers from creating unlimited
    cache keys and makes requests from different accounts converge on one row.
    """

    # Fixed-income instruments are commonly identified by product names such
    # as ``TESOURO IPCA+ 2029`` rather than exchange ticker syntax. The service
    # keeps strict ticker parsing for market-traded kinds after this bounded
    # field has normalized whitespace.
    ticker: str = Field(min_length=1, max_length=40)
    kind: QualityAssetKind
    profile: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_PROFILE_LENGTH,
        pattern=r"^[a-z_]+$",
    )
    # A ticker can be listed on more than one venue.  The venue is part of the
    # immutable identity whenever the caller knows it, otherwise ``None`` is
    # intentionally retained in the key rather than guessing a market.
    venue: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_VENUE_LENGTH,
        pattern=r"^[A-Za-z0-9._-]+$",
    )
    # Public directory metadata can be more descriptive than a market source's
    # abbreviated label (for example ``B3 ON``).  Both this name and ``profile``
    # are caller hints outside the idempotency key: ticker, kind, venue and
    # period own the global run, while the API derives calculation metadata
    # from provider evidence.
    corporate_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_CORPORATE_NAME_LENGTH,
    )
    period_at: datetime

    @field_validator("ticker", mode="before")
    @classmethod
    def normalize_ticker(cls, value: object) -> object:
        return " ".join(value.split()).upper() if isinstance(value, str) else value

    @field_validator("period_at")
    @classmethod
    def require_aware_scheduler_period(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("period_at must include a timezone offset")
        local = value.astimezone(ASSESSMENT_TIMEZONE)
        if (
            local.hour not in ASSESSMENT_HOURS
            or local.minute != 0
            or local.second != 0
            or local.microsecond != 0
        ):
            raise ValueError("period_at must be one of 12:00, 14:00, 16:00 or 19:00 local time")
        return value.astimezone(UTC)

    @field_validator("venue", mode="before")
    @classmethod
    def normalize_venue(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = value.strip().upper()
        # ``-`` was historically used as the serialized fallback for an
        # omitted venue.  Accepting it as a real venue makes two distinct
        # identities share one durable assessment key, so callers must omit
        # the field when the venue is unknown.
        if normalized == "-":
            raise ValueError("venue '-' is reserved; omit venue when unknown")
        return normalized

    @field_validator("corporate_name", mode="before")
    @classmethod
    def normalize_corporate_name(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = " ".join(value.split())
        if any(unicodedata.category(character).startswith("C") for character in normalized):
            raise ValueError("corporate_name contains unsupported control characters")
        return normalized


class AssessmentSnapshotResponse(BaseModel):
    ticker: str
    kind: QualityAssetKind
    profile: str | None = None
    venue: str | None = None
    period_at: datetime
    fetched_at: datetime | None = None
    status: AssessmentRunStatus
    evidence_digest: str | None = None
    opportunity: OpportunityResponse | None = None
    fundamentals: FundamentalsSnapshot | None = None
    quality: QualityAssetFacts | None = None
    components: dict[str, AssessmentComponentState] = Field(default_factory=dict)
    error: str | None = None
    retry_after_seconds: int | None = Field(default=None, ge=0)


def response_payload(response: AssessmentSnapshotResponse) -> dict[str, Any]:
    """Return a JSON-compatible payload suitable for durable storage."""

    return response.model_dump(mode="json")
