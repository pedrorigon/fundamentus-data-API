from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from io import TextIOWrapper
from zipfile import BadZipFile

import httpx

from app.config import Settings
from app.core.archive_safety import (
    ArchiveSafetyError,
    open_validated_zip,
    read_bounded_body,
)

SOURCE_B3_COTAHIST = "b3_cotahist"
_RECORD_TYPE = "01"
_CASH_MARKET = "010"
_PRICE_SCALE = Decimal("100")

_PriceSeries = dict[str, dict[date, Decimal]]


@dataclass(slots=True)
class _ParseWaiter:
    tickers: frozenset[str]
    future: asyncio.Future[_PriceSeries]


@dataclass(slots=True)
class _ParseFlight:
    year: int
    tickers: set[str] = field(default_factory=set)
    waiters: list[_ParseWaiter] = field(default_factory=list)
    accepting: bool = True
    payload: bytes | None = None
    task: asyncio.Task[None] | None = None


class B3HistoricalQuoteProvider:
    """Read B3's public annual COTAHIST archives for the requested tickers."""

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # Import lazily to keep the scraper package independent from the
        # services package's eager exports during module initialization.
        from app.services.singleflight import SingleFlight

        self.settings = settings
        self.transport = transport
        self._download_flight = SingleFlight()
        self._parse_flight_lock = asyncio.Lock()
        self._parse_flights: dict[int, _ParseFlight] = {}

    async def prices(self, year: int, tickers: set[str]) -> _PriceSeries:
        if not tickers:
            return {}
        normalized = _normalize_tickers(tickers)
        joined = await self._join_parse_flight(year, normalized)
        if isinstance(joined, bytes):
            # The active flight has already frozen its union.  Parsing this
            # late caller independently preserves its requested subset without
            # retaining parsed data after the download completes.
            return await asyncio.to_thread(parse_b3_historical_quotes, joined, set(normalized))
        try:
            return await asyncio.shield(joined)
        except asyncio.CancelledError:
            # Shield keeps cancellation from reaching the producer task. Mark
            # only this waiter as cancelled so ownership projection does not
            # retain a result nobody can consume.
            joined.cancel()
            raise

    async def _join_parse_flight(
        self,
        year: int,
        tickers: frozenset[str],
    ) -> asyncio.Future[_PriceSeries] | bytes:
        loop = asyncio.get_running_loop()
        async with self._parse_flight_lock:
            flight = self._parse_flights.get(year)
            if flight is not None and flight.accepting:
                return self._register_waiter(flight, tickers, loop)

            if flight is not None and flight.payload is not None:
                return flight.payload

            flight = _ParseFlight(year)
            self._parse_flights[year] = flight
            waiter = self._register_waiter(flight, tickers, loop)
            task = asyncio.create_task(self._produce_parse_flight(flight))
            flight.task = task
            task.add_done_callback(_consume_task_exception)
            return waiter

    @staticmethod
    def _register_waiter(
        flight: _ParseFlight,
        tickers: frozenset[str],
        loop: asyncio.AbstractEventLoop,
    ) -> asyncio.Future[_PriceSeries]:
        future: asyncio.Future[_PriceSeries] = loop.create_future()
        future.add_done_callback(_consume_future_exception)
        flight.tickers.update(tickers)
        flight.waiters.append(_ParseWaiter(tickers=tickers, future=future))
        return future

    async def _produce_parse_flight(self, flight: _ParseFlight) -> None:
        try:
            payload = await self._download(flight.year)
        except BaseException as error:
            await self._finish_parse_flight(flight, error=error)
            return

        async with self._parse_flight_lock:
            # Freeze the union only after the download has completed. Callers
            # arriving before this lock acquisition are included; later callers
            # receive the frozen bytes and parse their own subset.
            flight.accepting = False
            flight.payload = payload
            tickers = frozenset(flight.tickers)

        try:
            parsed = (
                {}
                if payload is None
                else await asyncio.to_thread(parse_b3_historical_quotes, payload, set(tickers))
            )
        except BaseException as error:
            await self._finish_parse_flight(flight, error=error)
            return

        owners = _last_series_owners(parsed, flight.waiters)
        async with self._parse_flight_lock:
            if self._parse_flights.get(flight.year) is flight:
                self._parse_flights.pop(flight.year, None)
            waiters = tuple(flight.waiters)
        for index, waiter in enumerate(waiters):
            if not waiter.future.done():
                waiter.future.set_result(
                    _project_prices(parsed, waiter.tickers, index=index, owners=owners)
                )

    async def _finish_parse_flight(
        self,
        flight: _ParseFlight,
        *,
        error: BaseException,
    ) -> None:
        async with self._parse_flight_lock:
            flight.accepting = False
            if self._parse_flights.get(flight.year) is flight:
                self._parse_flights.pop(flight.year, None)
            waiters = tuple(flight.waiters)
        for waiter in waiters:
            if not waiter.future.done():
                waiter.future.set_exception(error)

    async def _download(self, year: int) -> bytes | None:
        """Share one in-flight annual archive download among all ticker sets."""
        return await self._download_flight.run(
            f"b3-cotahist:{year}",
            lambda: self._request(year),
        )

    async def _request(self, year: int) -> bytes | None:
        """Download one annual archive while preserving provider error semantics."""
        filename = f"COTAHIST_A{year}.ZIP"
        async with httpx.AsyncClient(
            base_url=self.settings.b3_historical_quote_base_url,
            timeout=httpx.Timeout(self.settings.request_timeout_seconds),
            transport=self.transport,
            headers={"User-Agent": self.settings.user_agent},
        ) as client:
            async with client.stream("GET", f"/{filename}") as response:
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                try:
                    payload = await read_bounded_body(
                        response, self.settings.archive_download_max_bytes
                    )
                except ArchiveSafetyError:
                    return None
        return payload


def parse_b3_historical_quotes(payload: bytes, tickers: set[str]) -> _PriceSeries:
    """Parse COTAHIST records using B3's public fixed-width layout."""
    normalized = _normalize_tickers(tickers)
    if not normalized:
        return {}
    prices: dict[str, dict[date, Decimal]] = {ticker: {} for ticker in normalized}
    try:
        with open_validated_zip(payload) as archive:
            filename = next(name for name in archive.namelist() if not name.endswith("/"))
            with archive.open(filename) as raw:
                for line in TextIOWrapper(raw, encoding="latin-1"):
                    _add_price(prices, line)
    except (ArchiveSafetyError, BadZipFile, OSError, StopIteration, UnicodeError):
        return {}
    return {ticker: series for ticker, series in prices.items() if series}


def _normalize_tickers(tickers: set[str]) -> frozenset[str]:
    return frozenset(ticker.strip().upper() for ticker in tickers if ticker.strip())


def _last_series_owners(
    parsed: _PriceSeries,
    waiters: list[_ParseWaiter],
) -> dict[str, int]:
    owners: dict[str, int] = {}
    for index, waiter in enumerate(waiters):
        if waiter.future.cancelled():
            continue
        for ticker in waiter.tickers:
            if ticker in parsed:
                owners[ticker] = index
    return owners


def _project_prices(
    parsed: _PriceSeries,
    tickers: frozenset[str],
    *,
    index: int,
    owners: dict[str, int],
) -> _PriceSeries:
    projected: _PriceSeries = {}
    for ticker in tickers:
        series = parsed.get(ticker)
        if series is None:
            continue
        projected[ticker] = parsed.pop(ticker) if owners.get(ticker) == index else dict(series)
    return projected


def _consume_future_exception(future: asyncio.Future[_PriceSeries]) -> None:
    if not future.cancelled():
        future.exception()


def _consume_task_exception(task: asyncio.Task[None]) -> None:
    if not task.cancelled():
        task.exception()


def _add_price(prices: dict[str, dict[date, Decimal]], line: str) -> None:
    if line[:2] != _RECORD_TYPE or line[24:27] != _CASH_MARKET:
        return
    ticker = line[12:24].strip().upper()
    if ticker not in prices:
        return
    try:
        reference = date.fromisoformat(f"{line[2:6]}-{line[6:8]}-{line[8:10]}")
        last_price = Decimal(line[108:121]) / _PRICE_SCALE
        factor = Decimal(line[210:217])
    except (InvalidOperation, ValueError):
        return
    if last_price > 0 and factor > 0:
        prices[ticker][reference] = last_price / factor
