"""Public SND secondary-market trade history for Brazilian debentures."""

from __future__ import annotations

import asyncio
import csv
from collections import OrderedDict
from datetime import date, datetime
from decimal import Decimal
from io import StringIO
from time import monotonic

import httpx

from app.config import Settings
from app.core.archive_safety import ArchiveSafetyError, read_bounded_body
from app.scrapers.anbima_fixed_income import _decimal

SOURCE_SND_SECONDARY_MARKET = "snd_secondary_market"
_DOWNLOAD_PATH = "/exploreosnd/consultaadados/mercadosecundario/precosdenegociacao_e.asp"
_HEADER_PREFIX = "Data\tEmissor\tCódigo do Ativo\t"


class SndDebentureTradeProvider:
    """Resolve retained PUs from SND's public trade-history download.

    SND publishes the minimum, average and maximum PUs actually registered in
    the secondary market.  The average PU is an observed market value, not the
    contractual curve shown by SND's separate ``PU Histórico`` report.
    """

    source = SOURCE_SND_SECONDARY_MARKET
    identifier_scoped = True
    full_history = True

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self._series: OrderedDict[tuple[str, int], tuple[float, dict[date, Decimal]]] = (
            OrderedDict()
        )
        self._inflight: dict[tuple[str, int], asyncio.Task[dict[date, Decimal]]] = {}
        self._cache_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max(1, settings.upstream_concurrency))

    async def prices(self, _reference: date) -> dict[str, Decimal]:
        return {}

    async def prices_for(self, reference: date, identifiers: set[str]) -> dict[str, Decimal]:
        normalized = sorted({value.strip().upper() for value in identifiers if value.strip()})
        if not normalized or reference.year > date.today().year:
            return {}
        series = await asyncio.gather(
            *(self._price_series(identifier, reference.year) for identifier in normalized)
        )
        return {
            identifier: price
            for identifier, values in zip(normalized, series, strict=True)
            if (price := values.get(reference)) is not None
        }

    async def _price_series(self, identifier: str, year: int) -> dict[date, Decimal]:
        key = (identifier, year)
        async with self._cache_lock:
            cached = self._series.get(key)
            if cached is not None and cached[0] > monotonic():
                self._series.move_to_end(key)
                return cached[1]
            self._series.pop(key, None)
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(self._download_and_cache(identifier, year))
                self._inflight[key] = task
        try:
            return await task
        finally:
            async with self._cache_lock:
                if self._inflight.get(key) is task:
                    self._inflight.pop(key, None)

    async def _download_and_cache(self, identifier: str, year: int) -> dict[date, Decimal]:
        values = await self._download(identifier, year)
        ttl = (
            self.settings.fixed_income_current_ttl_seconds
            if year == date.today().year
            else self.settings.fixed_income_history_ttl_seconds
        )
        key = (identifier, year)
        async with self._cache_lock:
            self._series[key] = (monotonic() + ttl, values)
            self._series.move_to_end(key)
            while len(self._series) > self.settings.fixed_income_series_cache_max_entries:
                self._series.popitem(last=False)
        return values

    async def _download(self, identifier: str, year: int) -> dict[date, Decimal]:
        end = min(date(year, 12, 31), date.today())
        params = {
            "op_exc": "",
            "emissor": "",
            "isin": "",
            "ativo": identifier,
            "dt_ini": f"{year}0101",
            "dt_fim": end.strftime("%Y%m%d"),
        }
        async with self._semaphore:
            async with httpx.AsyncClient(
                base_url=self.settings.snd_debenture_base_url.rstrip("/"),
                timeout=httpx.Timeout(self.settings.request_timeout_seconds),
                transport=self.transport,
                headers={
                    "Accept": "text/plain, text/tab-separated-values",
                    "User-Agent": self.settings.user_agent,
                },
                follow_redirects=True,
            ) as client:
                async with client.stream("GET", _DOWNLOAD_PATH, params=params) as response:
                    if response.status_code == 404:
                        return {}
                    response.raise_for_status()
                    try:
                        payload = await read_bounded_body(
                            response,
                            self.settings.income_document_max_bytes,
                        )
                    except ArchiveSafetyError:
                        return {}
        return parse_snd_debenture_trade_prices(payload).get(identifier, {})


def parse_snd_debenture_trade_prices(payload: bytes) -> dict[str, dict[date, Decimal]]:
    """Parse SND's tab-delimited average traded PUs by instrument and date."""

    text = payload.decode("latin-1")
    header_index = text.find(_HEADER_PREFIX)
    if header_index < 0:
        return {}
    rows = csv.DictReader(StringIO(text[header_index:]), delimiter="\t")
    prices: dict[str, dict[date, Decimal]] = {}
    for row in rows:
        identifier = (row.get("Código do Ativo") or "").strip().upper()
        reference = _date(row.get("Data"))
        price = _decimal(row.get("PU Médio"))
        if identifier and reference is not None and price is not None and price > 0:
            prices.setdefault(identifier, {})[reference] = price
    return prices


def _date(value: str | None) -> date | None:
    try:
        return datetime.strptime((value or "").strip(), "%d/%m/%Y").date()
    except ValueError:
        return None
