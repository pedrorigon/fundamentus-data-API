from datetime import date
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class ValuationMethod(StrEnum):
    indicative = "indicative"


class FixedIncomeValuation(BaseModel):
    identifier: str
    reference_date: date
    unit_price: Decimal
    currency: str = "BRL"
    source: str
    method: ValuationMethod


class FixedIncomeValuationRequest(BaseModel):
    identifiers: list[str] = Field(min_length=1, max_length=50)
    dates: list[date] = Field(min_length=1, max_length=50)

    @field_validator("identifiers")
    @classmethod
    def normalize_identifiers(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            identifier = value.strip().upper()
            if not identifier or len(identifier) > 40 or not identifier.replace("-", "").isalnum():
                raise ValueError("invalid fixed-income identifier")
            if identifier not in normalized:
                normalized.append(identifier)
        return normalized

    @field_validator("dates")
    @classmethod
    def unique_dates(cls, values: list[date]) -> list[date]:
        return list(dict.fromkeys(values))


class FixedIncomeValuationResponse(BaseModel):
    valuations: dict[str, list[FixedIncomeValuation]]
    unavailable: list[str]
