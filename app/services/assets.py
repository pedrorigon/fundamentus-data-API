import asyncio
from collections.abc import Awaitable
from datetime import date
from typing import Protocol

from app.cache import CacheStore
from app.config import Settings
from app.core.errors import InvalidTickerError, LocalRateLimitError
from app.core.metrics import metrics
from app.core.time import today_in_timezone
from app.models import AssetDetails, AssetResponse, Dividend, DividendPeriod
from app.parsers import filter_dividends, parse_asset_details, parse_dividends
from app.parsers.normalizers import normalize_ticker
from app.services.singleflight import SingleFlight


class FundamentusSource(Protocol):
    def details_url(self, ticker: str) -> str: ...

    def fetch_details(self, ticker: str) -> Awaitable[str]: ...

    def fetch_dividends(self, ticker: str) -> Awaitable[str]: ...

    def fetch_fii_dividends(self, ticker: str) -> Awaitable[str]: ...


def _details_from_cache(value: object) -> AssetDetails:
    if isinstance(value, AssetDetails):
        return value
    return AssetDetails.model_validate(value)


def _dividends_from_cache(value: object) -> list[Dividend]:
    if isinstance(value, list) and all(isinstance(item, Dividend) for item in value):
        return value
    if isinstance(value, list):
        return [Dividend.model_validate(item) for item in value]
    return []


class AssetService:
    def __init__(self, scraper: FundamentusSource, cache: CacheStore, settings: Settings) -> None:
        self.scraper = scraper
        self.cache = cache
        self.settings = settings
        self.singleflight = SingleFlight()

    def normalize_ticker(self, ticker: str) -> str:
        try:
            return normalize_ticker(ticker)
        except ValueError as exc:
            raise InvalidTickerError(ticker=ticker) from exc

    async def get_details(
        self,
        ticker: str,
        *,
        force_refresh: bool = False,
    ) -> tuple[AssetDetails, bool]:
        normalized = self.normalize_ticker(ticker)
        cache_key = f"details:{normalized}"
        if not force_refresh:
            cached, hit = await self.cache.get(cache_key)
            if hit and cached is not None:
                metrics.inc("cache_hits")
                return _details_from_cache(cached), True
            metrics.inc("cache_misses")

        async def load() -> AssetDetails:
            html = await self.scraper.fetch_details(normalized)
            details = await asyncio.to_thread(
                parse_asset_details,
                html,
                normalized,
                self.scraper.details_url(normalized),
            )
            await self.cache.set(cache_key, details, self.settings.details_ttl_seconds)
            return details

        return await self.singleflight.run(cache_key, load), False

    async def get_dividends(
        self,
        ticker: str,
        *,
        period: DividendPeriod = DividendPeriod.all,
        as_of: date | None = None,
        force_refresh: bool = False,
    ) -> tuple[list[Dividend], bool]:
        normalized = self.normalize_ticker(ticker)
        cache_key = f"dividends:{normalized}"
        if not force_refresh:
            cached, hit = await self.cache.get(cache_key)
            if hit and cached is not None:
                metrics.inc("cache_hits")
                reference = as_of or today_in_timezone(self.settings.timezone)
                dividends = _dividends_from_cache(cached)
                return filter_dividends(dividends, period, reference), True
            metrics.inc("cache_misses")

        async def load() -> list[Dividend]:
            html = await self.scraper.fetch_dividends(normalized)
            dividends = await asyncio.to_thread(parse_dividends, html, normalized)
            if not dividends:
                html = await self.scraper.fetch_fii_dividends(normalized)
                dividends = await asyncio.to_thread(parse_dividends, html, normalized)
            await self.cache.set(cache_key, dividends, self.settings.dividends_ttl_seconds)
            return dividends

        loaded = await self.singleflight.run(cache_key, load)
        reference = as_of or today_in_timezone(self.settings.timezone)
        return filter_dividends(loaded, period, reference), False

    async def get_asset(
        self,
        ticker: str,
        *,
        include_details: bool = True,
        include_dividends: bool = True,
        period: DividendPeriod = DividendPeriod.all,
        as_of: date | None = None,
        force_refresh: bool = False,
    ) -> AssetResponse:
        normalized = self.normalize_ticker(ticker)
        details: AssetDetails | None = None
        dividends: list[Dividend] | None = None
        cached: dict[str, bool] = {}

        details_task = (
            asyncio.create_task(self.get_details(normalized, force_refresh=force_refresh))
            if include_details
            else None
        )
        dividends_task = (
            asyncio.create_task(
                self.get_dividends(
                    normalized,
                    period=period,
                    as_of=as_of,
                    force_refresh=force_refresh,
                )
            )
            if include_dividends
            else None
        )

        if details_task is not None:
            details, cached["details"] = await details_task
        if dividends_task is not None:
            dividends, cached["dividends"] = await dividends_task

        return AssetResponse(
            ticker=normalized,
            details=details,
            dividends=dividends,
            cached=cached,
        )

    async def get_batch(
        self,
        tickers: list[str],
        *,
        include_details: bool = True,
        include_dividends: bool = False,
        period: DividendPeriod = DividendPeriod.all,
        as_of: date | None = None,
        force_refresh: bool = False,
    ) -> list[AssetResponse]:
        if len(tickers) > self.settings.batch_limit:
            raise LocalRateLimitError(
                message=f"Local batch limit of {self.settings.batch_limit} tickers exceeded."
            )
        normalized = [self.normalize_ticker(ticker) for ticker in tickers]
        return await asyncio.gather(
            *[
                self.get_asset(
                    ticker,
                    include_details=include_details,
                    include_dividends=include_dividends,
                    period=period,
                    as_of=as_of,
                    force_refresh=force_refresh,
                )
                for ticker in normalized
            ]
        )
