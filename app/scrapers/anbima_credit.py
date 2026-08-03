"""Public ANBIMA Data prices for CRI and CRA certificates.

ANBIMA makes the last five business days of indicative CRI/CRA prices
available through a public HTML table.  This is deliberately a separate
provider from the authenticated ANBIMA Feed API: the public page is useful for
short-lived refreshes, while dates outside that window remain unavailable.
"""

from __future__ import annotations

import asyncio
import unicodedata
from datetime import date, datetime
from decimal import Decimal
from time import monotonic

import httpx
from selectolax.parser import HTMLParser

from app.config import Settings
from app.scrapers.anbima_fixed_income import _decimal

SOURCE_ANBIMA_CRI_CRA = "anbima_cri_cra"
_TABLE_SELECTOR = "table.custom-anbi-ui-table"
_CACHE_TTL_SECONDS = 3600.0


class AnbimaCreditProvider:
    """Resolve current CRI/CRA prices without requiring an API credential."""

    source = SOURCE_ANBIMA_CRI_CRA
    identifier_scoped = False

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self._cache_expires_at = 0.0
        self._prices_by_date: dict[date, dict[str, Decimal]] = {}
        self._refresh_lock = asyncio.Lock()

    async def prices(self, reference: date) -> dict[str, Decimal]:
        if monotonic() >= self._cache_expires_at:
            async with self._refresh_lock:
                if monotonic() >= self._cache_expires_at:
                    await self._refresh()
        return dict(self._prices_by_date.get(reference, {}))

    async def prices_for(self, reference: date, identifiers: set[str]) -> dict[str, Decimal]:
        prices = await self.prices(reference)
        wanted = {identifier.strip().upper() for identifier in identifiers}
        return {identifier: price for identifier, price in prices.items() if identifier in wanted}

    async def _refresh(self) -> None:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.settings.request_timeout_seconds),
            transport=self.transport,
            headers={
                "Accept": "text/html",
                "Accept-Language": "pt-BR,pt;q=0.9",
                "User-Agent": self.settings.user_agent,
            },
            follow_redirects=True,
        ) as client:
            response = await client.get(self.settings.anbima_credit_url)
        if response.status_code == 404:
            self._prices_by_date = {}
            self._cache_expires_at = monotonic() + _CACHE_TTL_SECONDS
            return
        response.raise_for_status()
        self._prices_by_date = parse_anbima_credit_price_series(response.content)
        self._cache_expires_at = monotonic() + _CACHE_TTL_SECONDS


def parse_anbima_credit_prices(
    payload: bytes,
    reference: date | None = None,
) -> dict[str, Decimal]:
    """Parse positive CRI/CRA PUs from the public ANBIMA table.

    ``reference`` filters the five-day table to one observation date.  When it
    is omitted all rows are returned, which is useful for provider caching and
    deterministic parser tests.
    """

    series = parse_anbima_credit_price_series(payload)
    if reference is None:
        result: dict[str, Decimal] = {}
        for prices in series.values():
            result.update(prices)
        return result
    return series.get(reference, {})


def parse_anbima_credit_price_series(payload: bytes) -> dict[date, dict[str, Decimal]]:
    """Return ``reference date -> identifier -> PU`` from ANBIMA HTML."""

    document = HTMLParser(payload.decode("utf-8", errors="replace"))
    for table in document.css(_TABLE_SELECTOR):
        rows = table.css("tr")
        if not rows:
            continue
        header = [_normalize_header(cell.text(strip=True)) for cell in rows[0].css("th, td")]
        indexes = _column_indexes(header)
        if indexes is None:
            continue
        result: dict[date, dict[str, Decimal]] = {}
        for row in rows[1:]:
            values = [cell.text(strip=True) for cell in row.css("th, td")]
            if len(values) <= max(indexes.values()):
                continue
            reference = _date(values[indexes["reference"]])
            identifier = values[indexes["identifier"]].strip().upper()
            price = _decimal(values[indexes["price"]])
            if reference is None or not identifier or price is None or price <= 0:
                continue
            result.setdefault(reference, {})[identifier] = price
        return result
    return {}


def _column_indexes(header: list[str]) -> dict[str, int] | None:
    aliases = {
        "reference": {"data de referencia", "data referencia"},
        "identifier": {"codigo", "codigo do ativo"},
        "price": {"pu", "pu indicativo"},
    }
    indexes: dict[str, int] = {}
    for key, accepted in aliases.items():
        for index, value in enumerate(header):
            if value in accepted:
                indexes[key] = index
                break
    return indexes if len(indexes) == len(aliases) else None


def _normalize_header(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(char for char in normalized if not unicodedata.combining(char)).casefold()


def _date(value: str) -> date | None:
    try:
        return datetime.strptime(value.strip(), "%d/%m/%Y").date()
    except ValueError:
        return None
