from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from io import BytesIO, TextIOWrapper
from zipfile import BadZipFile, ZipFile

import httpx

from app.config import Settings

SOURCE_B3_COTAHIST = "b3_cotahist"
_RECORD_TYPE = "01"
_CASH_MARKET = "010"
_PRICE_SCALE = Decimal("100")


class B3HistoricalQuoteProvider:
    """Read B3's public annual COTAHIST archives for the requested tickers."""

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport

    async def prices(self, year: int, tickers: set[str]) -> dict[str, dict[date, Decimal]]:
        if not tickers:
            return {}
        filename = f"COTAHIST_A{year}.ZIP"
        async with httpx.AsyncClient(
            base_url=self.settings.b3_historical_quote_base_url,
            timeout=httpx.Timeout(self.settings.request_timeout_seconds),
            transport=self.transport,
            headers={"User-Agent": self.settings.user_agent},
        ) as client:
            response = await client.get(f"/{filename}")
        if response.status_code == 404:
            return {}
        response.raise_for_status()
        return parse_b3_historical_quotes(response.content, tickers)


def parse_b3_historical_quotes(payload: bytes, tickers: set[str]) -> dict[str, dict[date, Decimal]]:
    """Parse COTAHIST records using B3's public fixed-width layout."""
    normalized = {ticker.strip().upper() for ticker in tickers if ticker.strip()}
    if not normalized:
        return {}
    prices: dict[str, dict[date, Decimal]] = {ticker: {} for ticker in normalized}
    try:
        with ZipFile(BytesIO(payload)) as archive:
            filename = next(name for name in archive.namelist() if not name.endswith("/"))
            with archive.open(filename) as raw:
                for line in TextIOWrapper(raw, encoding="latin-1"):
                    _add_price(prices, line)
    except (BadZipFile, OSError, StopIteration, UnicodeError):
        return {}
    return {ticker: series for ticker, series in prices.items() if series}


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
