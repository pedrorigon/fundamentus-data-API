from datetime import date
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_FIXED_INCOME_DATES = 100
MAX_FIXED_INCOME_LOOKUPS = 500


class ValuationMethod(StrEnum):
    indicative = "indicative"


class FixedIncomeValuation(BaseModel):
    identifier: str
    requested_date: date
    reference_date: date
    unit_price: Decimal
    currency: str = "BRL"
    source: str
    method: ValuationMethod


class FixedIncomeValuationRequest(BaseModel):
    identifiers: list[str] = Field(min_length=1, max_length=50)
    dates: list[date] = Field(min_length=1, max_length=MAX_FIXED_INCOME_DATES)

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

    @model_validator(mode="after")
    def bounded_work(self) -> "FixedIncomeValuationRequest":
        if len(self.identifiers) * len(self.dates) > MAX_FIXED_INCOME_LOOKUPS:
            raise ValueError("fixed-income request exceeds the lookup budget")
        return self


class FixedIncomeValuationResponse(BaseModel):
    valuations: dict[str, list[FixedIncomeValuation]]
    unavailable: list[str]
    unavailable_reasons: dict[str, str] = Field(default_factory=dict)
