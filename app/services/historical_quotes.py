from __future__ import annotations

import asyncio
from bisect import bisect_right
from datetime import date
from decimal import Decimal, InvalidOperation

import httpx

from app.cache import CacheStore
from app.config import Settings
from app.models.historical_quotes import (
    HistoricalQuote,
    HistoricalQuoteRequest,
    HistoricalQuoteResponse,
)
from app.scrapers.b3_historical_quotes import SOURCE_B3_COTAHIST, B3HistoricalQuoteProvider


class HistoricalQuoteService:
    def __init__(
        self,
        settings: Settings,
        cache: CacheStore,
        provider: B3HistoricalQuoteProvider | None = None,
    ) -> None:
        self.settings = settings
        self.cache = cache
        self.provider = provider or B3HistoricalQuoteProvider(settings)

    async def resolve(self, request: HistoricalQuoteRequest) -> HistoricalQuoteResponse:
        years = {target.year for target in request.dates}
        years.add(min(years) - 1)
        series = await self._series(request.tickers, years)
        quotes = {
            ticker: self._resolve_ticker(ticker, series.get(ticker, {}), request.dates)
            for ticker in request.tickers
        }
        unavailable = [ticker for ticker, values in quotes.items() if not values]
        return HistoricalQuoteResponse(quotes=quotes, unavailable=unavailable)

    async def _series(
        self,
        tickers: list[str],
        years: set[int],
    ) -> dict[str, dict[date, Decimal]]:
        result: dict[str, dict[date, Decimal]] = {ticker: {} for ticker in tickers}
        concurrency = max(1, self.settings.upstream_concurrency)
        semaphore = asyncio.Semaphore(concurrency)

        async def load(year: int) -> dict[str, dict[date, Decimal]]:
            async with semaphore:
                return await self._year_series(tickers, year)

        loaded_years = await asyncio.gather(*(load(year) for year in sorted(years)))
        for loaded in loaded_years:
            for ticker, values in loaded.items():
                result[ticker].update(values)
        return result

    async def _year_series(
        self,
        tickers: list[str],
        year: int,
    ) -> dict[str, dict[date, Decimal]]:
        result, missing = await self._cached_year(tickers, year)
        if not missing:
            return result
        try:
            loaded = await self.provider.prices(year, set(missing))
        except (httpx.HTTPError, ValueError):
            loaded = {}
        for ticker in missing:
            values = loaded.get(ticker, {})
            await self.cache.set(
                _cache_key(year, ticker),
                {day.isoformat(): str(price) for day, price in values.items()},
                self.settings.equity_history_ttl_seconds
                if values
                else self.settings.market_data_ttl_seconds,
            )
            result[ticker] = values
        return result

    async def _cached_year(
        self,
        tickers: list[str],
        year: int,
    ) -> tuple[dict[str, dict[date, Decimal]], list[str]]:
        cached: dict[str, dict[date, Decimal]] = {}
        missing: list[str] = []
        for ticker in tickers:
            value, found = await self.cache.get(_cache_key(year, ticker))
            if not found:
                missing.append(ticker)
                continue
            cached[ticker] = _cached_series(value)
        return cached, missing

    @staticmethod
    def _resolve_ticker(
        ticker: str,
        series: dict[date, Decimal],
        targets: list[date],
    ) -> list[HistoricalQuote]:
        values: list[HistoricalQuote] = []
        ordered = sorted(series.items())
        ordered_dates = [reference for reference, _price in ordered]
        for target in targets:
            index = bisect_right(ordered_dates, target) - 1
            if index < 0:
                continue
            reference, price = ordered[index]
            values.append(
                HistoricalQuote(
                    ticker=ticker,
                    requested_date=target,
                    reference_date=reference,
                    close_price=price,
                    source=SOURCE_B3_COTAHIST,
                )
            )
        return values


def _cache_key(year: int, ticker: str) -> str:
    return f"equity-history:b3-cotahist:{year}:{ticker}"


def _cached_series(value: object) -> dict[date, Decimal]:
    if not isinstance(value, dict):
        return {}
    result: dict[date, Decimal] = {}
    for raw_day, raw_price in value.items():
        try:
            day = date.fromisoformat(str(raw_day))
            price = Decimal(str(raw_price))
        except (InvalidOperation, ValueError):
            continue
        if price > 0:
            result[day] = price
    return result
