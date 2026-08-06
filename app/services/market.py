from __future__ import annotations

import asyncio
import re
import unicodedata
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, cast

import httpx

from app.config import Settings
from app.core.errors import InvalidTickerError
from app.models import (
    FundAllocation,
    FundHolding,
    FundProfile,
    InstrumentDataResponse,
    InstrumentMetadata,
    InstrumentSearchResponse,
    InstrumentType,
    InternationalFundamentals,
    MarketQuote,
)
from app.scrapers.sec_companyfacts import SEC_TICKER_DIRECTORY, SecCompanyFactsProvider
from app.services.instrument_directory import (
    SOURCE_BRAPI_DIRECTORY,
    BrapiInstrumentDirectoryProvider,
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
        sec: SecCompanyFactsProvider | None = None,
        brapi_directory: BrapiInstrumentDirectoryProvider | None = None,
        directory_provider: BrapiInstrumentDirectoryProvider | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.b3 = b3 or B3InstrumentProvider(settings, transport)
        self.brapi = brapi or BrapiInstrumentDataProvider(settings, transport)
        self.alpha = alpha or AlphaVantageInstrumentDataProvider(settings, transport)
        self.sec = sec or SecCompanyFactsProvider(settings, transport)
        self.brapi_directory = (
            brapi_directory
            or directory_provider
            or BrapiInstrumentDirectoryProvider(settings, transport)
        )
        self._cache: dict[
            tuple[str, InstrumentType | None], tuple[datetime, InstrumentDataResponse]
        ] = {}
        self._directory: dict[tuple[str, str], InstrumentMetadata] = {}
        self._directory_lock = asyncio.Lock()
        self._directory_task: asyncio.Task[None] | None = None
        self._directory_refreshed_at = 0.0
        self._directory_loaded = False
        self._directory_warnings: list[str] = []

    async def warm_directory(self, *, force: bool = False) -> None:
        """Refresh bulk identity indexes without delaying application startup."""
        now = asyncio.get_running_loop().time()
        if (
            not force
            and self._directory_loaded
            and now - self._directory_refreshed_at < self.settings.instrument_directory_ttl_seconds
        ):
            return
        async with self._directory_lock:
            task = self._directory_task
            if task is None:
                task = asyncio.create_task(self._refresh_directory())
                self._directory_task = task
        try:
            await task
        finally:
            async with self._directory_lock:
                if self._directory_task is task:
                    self._directory_task = None

    async def refresh_directory(self) -> None:
        """Force a bounded bulk refresh, retaining the prior snapshot on failure."""
        await self.warm_directory(force=True)

    async def close(self) -> None:
        """Cancel a still-running background warm-up during application shutdown."""
        async with self._directory_lock:
            task = self._directory_task
            self._directory_task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

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
        if result.instrument is not None:
            self._remember(result.instrument)
        return result

    async def search(self, query: str, *, limit: int = 20) -> InstrumentSearchResponse:
        """Search the locally warmed/observed directory without network I/O."""
        normalized_query = _fold_search(query)
        bounded_limit = max(1, min(limit, 50))
        if not normalized_query:
            return InstrumentSearchResponse(
                query=query.strip(),
                limited=True,
                unavailable_reason="A non-empty search query is required",
            )
        matches = [
            item for item in self._directory.values() if _search_match(item, normalized_query)
        ]
        matches.sort(key=lambda item: _search_rank(item, normalized_query))
        return InstrumentSearchResponse(
            query=query.strip(),
            results=matches[:bounded_limit],
            limited=True,
            unavailable_reason=self._directory_message(bool(matches)),
        )

    async def _refresh_directory(self) -> None:
        sources: list[InstrumentMetadata] = []
        warnings: list[str] = []
        sec_task = asyncio.create_task(
            _invoke_directory(cast(Any, self.sec), "ticker_directory", "directory")
        )
        brapi_task = asyncio.create_task(
            _invoke_directory(cast(Any, self.brapi_directory), "directory", "instruments")
        )
        gathered: tuple[
            list[Any] | BaseException, list[Any] | BaseException
        ] = await asyncio.gather(sec_task, brapi_task, return_exceptions=True)
        sec_result, brapi_result = gathered
        if isinstance(sec_result, BaseException):
            warnings.append(f"{SEC_TICKER_DIRECTORY} unavailable: {sec_result}")
        else:
            sources.extend(_metadata_from_sec(item) for item in sec_result)
            if not sec_result:
                warnings.append(f"{SEC_TICKER_DIRECTORY} returned no records")
        if isinstance(brapi_result, BaseException):
            warnings.append(f"{SOURCE_BRAPI_DIRECTORY} unavailable: {brapi_result}")
        else:
            sources.extend(brapi_result)
            if not brapi_result:
                reason = getattr(self.brapi_directory, "last_error", None) or "returned no records"
                warnings.append(f"{SOURCE_BRAPI_DIRECTORY} unavailable: {reason}")
        if sources:
            for item in sources:
                self._remember(item)
            self._link_underlying_names()
            self._directory_loaded = True
            self._directory_refreshed_at = asyncio.get_running_loop().time()
        self._directory_warnings = warnings

    def _link_underlying_names(self) -> None:
        """Give each depositary receipt the issuer name of its underlying.

        The B3 list names a BDR after its own code, so nothing in that record
        mentions the company and a search for "NVIDIA" could not reach NVDC34.
        The underlying is indexed separately under the issuer's real name, so
        borrowing it is enough — and only when the receipt has no name of its
        own beyond the code.
        """
        names = {
            _fold_search(item.ticker): item.name
            for item in self._directory.values()
            if item.name and _fold_search(item.name) != _fold_search(item.ticker)
        }
        for key, item in self._directory.items():
            if item.underlying_name or not item.underlying_ticker:
                continue
            name = names.get(_fold_search(item.underlying_ticker))
            if name:
                self._directory[key] = item.model_copy(update={"underlying_name": name})

    def _remember(self, instrument: InstrumentMetadata) -> None:
        key = (_fold_search(instrument.ticker), _fold_search(instrument.exchange) or "")
        existing = self._directory.get(key)
        self._directory[key] = _merge_instruments(existing, instrument)

    def _directory_message(self, has_matches: bool) -> str:
        if self._directory_warnings:
            unavailable = "; ".join(self._directory_warnings)
            return f"Coverage is limited to available local indexes; {unavailable}"
        if not self._directory_loaded:
            return (
                "Directory warm-up is pending; results are limited to instruments "
                "already resolved by this process"
            )
        return (
            "Coverage is limited to SEC EDGAR and the complementary brapi B3 list"
            if has_matches
            else (
                "No local directory match; coverage is limited to SEC EDGAR and the "
                "complementary brapi B3 list"
            )
        )

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
            ticker,
            instrument,
            quote,
            fund_profile,
            fundamentals,
            refreshed_at,
            unavailable_reason=(instrument.underlying_unavailable_reason if instrument else None),
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
    unavailable_reason: str | None = None,
) -> InstrumentDataResponse:
    return InstrumentDataResponse(
        ticker=ticker,
        instrument=instrument,
        quote=quote,
        fund_profile=fund_profile,
        fundamentals=fundamentals,
        unavailable_reason=unavailable_reason,
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
        name=fundamentals.description if fundamentals else None,
        exchange=fundamentals.exchange if fundamentals else None,
        country=fundamentals.country if fundamentals else None,
        currency=fundamentals.currency if fundamentals else None,
        source=SOURCE_ALPHA_VANTAGE,
        confidence="medium",
    )


def _fold_search(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    return normalized.encode("ascii", "ignore").decode("ascii").upper().strip()


def _metadata_from_sec(record: Any) -> InstrumentMetadata:
    if isinstance(record, InstrumentMetadata):
        return record
    if isinstance(record, dict):
        ticker = str(record.get("ticker") or "")
        name = record.get("name")
        security_class = record.get("security_class") or record.get("class")
        exchange = record.get("exchange")
        country = record.get("country") or "US"
        cik = record.get("cik") or record.get("cik_str") or ""
    else:
        ticker = record.ticker
        name = record.name
        security_class = getattr(record, "security_class", None)
        exchange = getattr(record, "exchange", None)
        country = getattr(record, "country", None) or "US"
        cik = getattr(record, "cik", "")
    identifiers = {"cik": cik} if cik else {}
    return InstrumentMetadata(
        ticker=ticker,
        name=name,
        instrument_type=InstrumentType.stock,
        category=security_class or "common_stock",
        identifiers=identifiers,
        exchange=exchange.upper() if exchange else None,
        country=country.upper(),
        source=SEC_TICKER_DIRECTORY,
        confidence="high",
    )


async def _invoke_directory(provider: Any, preferred: str, fallback: str) -> list[Any]:
    loader = getattr(provider, preferred, None) or getattr(provider, fallback, None)
    if loader is None:
        raise RuntimeError(f"Directory provider has no {preferred}/{fallback} method")
    result = await loader()
    return result if isinstance(result, list) else []


def _merge_instruments(
    existing: InstrumentMetadata | None,
    incoming: InstrumentMetadata,
) -> InstrumentMetadata:
    if existing is None:
        return incoming
    values = incoming.model_dump()
    previous = existing.model_dump()
    for field, value in previous.items():
        if field in {"ticker", "instrument_type", "source", "confidence"}:
            continue
        if value not in (None, "", {}, []):
            if values.get(field) in (None, "", {}, []):
                values[field] = value
    if _confidence_rank(existing.confidence) > _confidence_rank(incoming.confidence):
        for field in (
            "instrument_type",
            "source",
            "confidence",
            "category",
            "cfi_code",
            "isin",
            "identifiers",
            "underlying_ticker",
            "underlying_name",
            "underlying_exchange",
            "underlying_country",
            "underlying_identifiers",
            "underlying_source",
            "underlying_unavailable_reason",
        ):
            values[field] = previous[field]
    # An observed BDR response can carry richer underlying metadata than a
    # bulk index; preserve it while retaining the freshest directory identity.
    return InstrumentMetadata(**values)


def _confidence_rank(value: str) -> int:
    return {"high": 2, "medium": 1, "low": 0}.get(value.lower(), -1)


def _search_match(item: InstrumentMetadata, query: str) -> bool:
    fields = (
        item.ticker,
        item.name,
        item.underlying_ticker,
        item.underlying_name,
    )
    return any(query in _fold_search(value) for value in fields if value)


def _search_rank(item: InstrumentMetadata, query: str) -> tuple[int, int, str, str]:
    ticker = _fold_search(item.ticker)
    name = _fold_search(item.name)
    underlying = _fold_search(item.underlying_ticker)
    if ticker == query:
        rank = 0
    elif ticker.startswith(query):
        rank = 1
    elif name.startswith(query):
        rank = 2
    elif underlying == query:
        rank = 3
    elif underlying.startswith(query):
        rank = 4
    else:
        rank = 5
    return rank, len(ticker), ticker, _fold_search(item.exchange)
