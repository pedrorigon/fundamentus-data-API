from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.config import Settings
from app.core.errors import InvalidTickerError
from app.models import (
    FundAllocation,
    FundHolding,
    FundProfile,
    InstrumentDataResponse,
    InstrumentMetadata,
    InstrumentType,
    InternationalFundamentals,
    MarketQuote,
)
from app.services.opportunity import B3InstrumentProvider

SOURCE_ALPHA_VANTAGE = "alpha_vantage"
SOURCE_BRAPI = "brapi"
INSTRUMENT_TICKER_PATTERN = re.compile(r"^[A-Z0-9]{1,8}(?:[.-][A-Z0-9]{1,3})?$")
B3_TICKER_PATTERN = re.compile(r"^[A-Z]{4}\d{1,2}$")


def _decimal(value: Any) -> Decimal | None:
    if value in {None, "", "None", "-"}:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _text(value: Any) -> str | None:
    cleaned = str(value).strip() if value is not None else ""
    return cleaned or None


def _datetime(value: Any) -> datetime | None:
    text = _text(value)
    if text is None:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


class BrapiInstrumentDataProvider:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport

    async def get(self, ticker: str) -> tuple[MarketQuote | None, InternationalFundamentals | None]:
        payload = await self._payload(ticker)
        if payload is None:
            return None, None
        data = _first_result_data(payload)
        if data is None:
            return None, None
        return _brapi_quote(data), _brapi_fundamentals(data)

    async def _payload(self, ticker: str) -> Any | None:
        headers = {}
        token = (
            self.settings.brapi_token.get_secret_value().strip()
            if self.settings.brapi_token
            else ""
        )
        if token:
            headers["Authorization"] = f"Bearer {token}"
        async with httpx.AsyncClient(
            base_url=self.settings.brapi_base_url,
            timeout=httpx.Timeout(self.settings.request_timeout_seconds),
            transport=self.transport,
            headers=headers,
        ) as client:
            response = await client.get("/api/v2/stocks/quote", params={"symbols": ticker})
            if response.status_code in {401, 403, 404, 429}:
                return None
            response.raise_for_status()
            return response.json()


class AlphaVantageInstrumentDataProvider:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport

    async def get(
        self,
        ticker: str,
        instrument_type: InstrumentType,
    ) -> tuple[FundProfile | None, InternationalFundamentals | None]:
        api_key = self.settings.alpha_vantage_api_key
        normalized_key = api_key.get_secret_value().strip() if api_key else ""
        if not normalized_key:
            return None, None
        payload = await self._payload(ticker, instrument_type, normalized_key)
        if payload is None:
            return None, None
        if instrument_type is InstrumentType.etf:
            return _alpha_fund_profile(payload), None
        return None, _alpha_fundamentals(payload)

    async def _payload(
        self,
        ticker: str,
        instrument_type: InstrumentType,
        api_key: str,
    ) -> dict[str, Any] | None:
        params = _alpha_params(ticker, instrument_type, api_key)
        async with httpx.AsyncClient(
            base_url=self.settings.alpha_vantage_base_url,
            timeout=httpx.Timeout(self.settings.request_timeout_seconds),
            transport=self.transport,
        ) as client:
            response = await client.get("/query", params=params)
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict) or payload.get("Error Message") or payload.get("Note"):
            return None
        return payload


class InstrumentDataService:
    def __init__(
        self,
        settings: Settings,
        *,
        b3: B3InstrumentProvider | None = None,
        brapi: BrapiInstrumentDataProvider | None = None,
        alpha: AlphaVantageInstrumentDataProvider | None = None,
    ) -> None:
        self.settings = settings
        self.b3 = b3 or B3InstrumentProvider(settings)
        self.brapi = brapi or BrapiInstrumentDataProvider(settings)
        self.alpha = alpha or AlphaVantageInstrumentDataProvider(settings)
        self._cache: dict[
            tuple[str, InstrumentType | None], tuple[datetime, InstrumentDataResponse]
        ] = {}

    async def get(
        self,
        ticker: str,
        instrument_type: InstrumentType | None = None,
    ) -> InstrumentDataResponse:
        normalized = _normalized_instrument_ticker(ticker)
        cache_key = (normalized, instrument_type)
        now = datetime.now(UTC)
        cached = self._cached(cache_key, now)
        if cached is not None:
            return cached
        result = await self._load(normalized, instrument_type, now)
        self._cache[cache_key] = (now, result)
        return result

    def _cached(
        self,
        cache_key: tuple[str, InstrumentType | None],
        now: datetime,
    ) -> InstrumentDataResponse | None:
        cached = self._cache.get(cache_key)
        if cached and (now - cached[0]).total_seconds() < self.settings.instrument_data_ttl_seconds:
            return cached[1]
        return None

    async def _load(
        self,
        ticker: str,
        instrument_type: InstrumentType | None,
        refreshed_at: datetime,
    ) -> InstrumentDataResponse:
        instrument = await self.b3.get(ticker) if B3_TICKER_PATTERN.fullmatch(ticker) else None
        resolved_type = (
            instrument.instrument_type if instrument else instrument_type or InstrumentType.stock
        )
        quote, fund_profile, fundamentals, instrument = await self._provider_data(
            ticker, instrument, resolved_type
        )
        return _instrument_response(
            ticker, instrument, quote, fund_profile, fundamentals, refreshed_at
        )

    async def _provider_data(
        self,
        ticker: str,
        instrument: InstrumentMetadata | None,
        resolved_type: InstrumentType,
    ) -> tuple[
        MarketQuote | None,
        FundProfile | None,
        InternationalFundamentals | None,
        InstrumentMetadata,
    ]:
        if instrument is not None:
            quote, fundamentals = await self.brapi.get(ticker)
            return quote, None, fundamentals, instrument
        fund_profile, fundamentals = await self.alpha.get(ticker, resolved_type)
        resolved_instrument = _international_instrument(ticker, resolved_type, fundamentals)
        return None, fund_profile, fundamentals, resolved_instrument


def _first_result_data(payload: Any) -> dict[str, Any] | None:
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        return None
    data = results[0].get("data")
    return data if isinstance(data, dict) else None


def _normalized_instrument_ticker(ticker: str) -> str:
    normalized = ticker.strip().upper()
    if INSTRUMENT_TICKER_PATTERN.fullmatch(normalized) is None:
        raise InvalidTickerError(ticker=normalized)
    return normalized


def _alpha_params(ticker: str, instrument_type: InstrumentType, api_key: str) -> dict[str, str]:
    function = "ETF_PROFILE" if instrument_type is InstrumentType.etf else "OVERVIEW"
    return {"function": function, "symbol": ticker, "apikey": api_key}


def _instrument_response(
    ticker: str,
    instrument: InstrumentMetadata,
    quote: MarketQuote | None,
    fund_profile: FundProfile | None,
    fundamentals: InternationalFundamentals | None,
    refreshed_at: datetime,
) -> InstrumentDataResponse:
    return InstrumentDataResponse(
        ticker=ticker,
        instrument=instrument,
        quote=quote,
        fund_profile=fund_profile,
        fundamentals=fundamentals,
        refreshed_at=refreshed_at,
    )


def _brapi_quote(data: dict[str, Any]) -> MarketQuote | None:
    price = _decimal(data.get("regularMarketPrice"))
    if price is None:
        return None
    return MarketQuote(
        price=price,
        currency=_text(data.get("currency")) or "BRL",
        exchange="B3",
        quoted_at=_datetime(data.get("regularMarketTime")),
        source=SOURCE_BRAPI,
    )


def _brapi_fundamentals(data: dict[str, Any]) -> InternationalFundamentals:
    return InternationalFundamentals(
        description=_text(data.get("longName")),
        exchange="B3",
        currency=_text(data.get("currency")) or "BRL",
        market_capitalization=_decimal(data.get("marketCap")),
        source=SOURCE_BRAPI,
    )


def _allocations(payload: Any) -> list[FundAllocation]:
    if not isinstance(payload, list):
        return []
    return [
        FundAllocation(name=str(item.get("name") or item.get("sector") or ""), weight=weight)
        for item in payload
        if isinstance(item, dict)
        if (weight := _decimal(item.get("weight"))) is not None
    ]


def _holdings(payload: Any) -> list[FundHolding]:
    if not isinstance(payload, list):
        return []
    return [
        FundHolding(
            symbol=str(item.get("symbol") or ""),
            description=_text(item.get("description")),
            weight=weight,
        )
        for item in payload
        if isinstance(item, dict) and item.get("symbol")
        if (weight := _decimal(item.get("weight"))) is not None
    ]


def _alpha_fund_profile(payload: dict[str, Any]) -> FundProfile:
    return FundProfile(
        net_assets=_decimal(payload.get("net_assets")),
        net_expense_ratio=_decimal(payload.get("net_expense_ratio")),
        portfolio_turnover=_decimal(payload.get("portfolio_turnover")),
        dividend_yield=_decimal(payload.get("dividend_yield")),
        nav=_decimal(payload.get("net_asset_value")),
        inception_date=_date(payload.get("inception_date")),
        description=_text(payload.get("description")),
        sectors=_allocations(payload.get("sectors")),
        asset_types=_allocations(payload.get("asset_allocation")),
        holdings=_holdings(payload.get("holdings")),
        source=SOURCE_ALPHA_VANTAGE,
    )


def _alpha_fundamentals(payload: dict[str, Any]) -> InternationalFundamentals:
    return InternationalFundamentals(
        description=_text(payload.get("Description")),
        country=_text(payload.get("Country")),
        sector=_text(payload.get("Sector")),
        industry=_text(payload.get("Industry")),
        exchange=_text(payload.get("Exchange")),
        currency=_text(payload.get("Currency")),
        market_capitalization=_decimal(payload.get("MarketCapitalization")),
        price_to_earnings=_decimal(payload.get("PERatio")),
        price_to_book=_decimal(payload.get("PriceToBookRatio")),
        earnings_per_share=_decimal(payload.get("EPS")),
        dividend_yield=_decimal(payload.get("DividendYield")),
        source=SOURCE_ALPHA_VANTAGE,
    )


def _international_instrument(
    ticker: str,
    instrument_type: InstrumentType,
    fundamentals: InternationalFundamentals | None,
) -> InstrumentMetadata:
    return InstrumentMetadata(
        ticker=ticker,
        instrument_type=instrument_type,
        category="INTERNATIONAL",
        currency=fundamentals.currency if fundamentals else None,
        source=SOURCE_ALPHA_VANTAGE,
        confidence="medium",
    )
