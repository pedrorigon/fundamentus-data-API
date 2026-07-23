from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

MAX_HISTORICAL_QUOTE_DATES = 2500


class HistoricalQuote(BaseModel):
    ticker: str
    requested_date: date
    reference_date: date
    close_price: Decimal
    currency: str = "BRL"
    source: str


class HistoricalQuoteRequest(BaseModel):
    tickers: list[str] = Field(min_length=1, max_length=50)
    dates: list[date] = Field(min_length=1, max_length=MAX_HISTORICAL_QUOTE_DATES)

    @field_validator("tickers")
    @classmethod
    def normalize_tickers(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            ticker = value.strip().upper()
            if not ticker or len(ticker) > 12 or not ticker.isalnum():
                raise ValueError("invalid ticker")
            if ticker not in normalized:
                normalized.append(ticker)
        return normalized

    @field_validator("dates")
    @classmethod
    def unique_dates(cls, values: list[date]) -> list[date]:
        return list(dict.fromkeys(values))


class HistoricalQuoteResponse(BaseModel):
    quotes: dict[str, list[HistoricalQuote]]
    unavailable: list[str]
