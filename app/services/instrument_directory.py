"""Bounded, local instrument directory adapters.

Directory adapters only perform bulk refreshes.  Search consumes the merged
in-memory snapshot and therefore never performs an upstream request per key
stroke.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx

from app.config import Settings
from app.models import InstrumentMetadata, InstrumentType

SOURCE_BRAPI_DIRECTORY = "brapi_directory_complementary"
_TICKER_PATTERN = re.compile(r"^[A-Z][A-Z0-9.-]{0,11}$")


class BrapiInstrumentDirectoryProvider:
    """Read brapi's public bounded quote list as a complementary B3 index."""

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self.last_error: str | None = None
        self._inflight: asyncio.Task[list[InstrumentMetadata]] | None = None
        self._lock = asyncio.Lock()

    async def directory(self) -> list[InstrumentMetadata]:
        """Fetch and parse one list response, sharing concurrent callers."""
        async with self._lock:
            task = self._inflight
            if task is None:
                task = asyncio.create_task(self._load())
                self._inflight = task
        try:
            return await task
        finally:
            async with self._lock:
                if self._inflight is task:
                    self._inflight = None

    async def instruments(self) -> list[InstrumentMetadata]:
        return await self.directory()

    async def _load(self) -> list[InstrumentMetadata]:
        self.last_error = None
        headers = {"Accept": "application/json", "User-Agent": self.settings.user_agent}
        try:
            async with httpx.AsyncClient(
                base_url=self.settings.brapi_base_url,
                timeout=httpx.Timeout(self.settings.request_timeout_seconds),
                transport=self.transport,
                headers=headers,
            ) as client:
                response = await client.get(
                    self.settings.brapi_instrument_directory_path,
                    params={"limit": self.settings.instrument_directory_max_rows},
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            self.last_error = str(exc) or exc.__class__.__name__
            return []
        records = parse_brapi_directory(payload)
        if not records:
            self.last_error = "brapi returned no parseable instruments"
        return records


def parse_brapi_directory(payload: Any) -> list[InstrumentMetadata]:
    """Parse the several list shapes used by brapi's public API over time."""
    rows = _rows(payload)
    result: list[InstrumentMetadata] = []
    seen: set[str] = set()
    for row in rows:
        ticker = _text(row, "stock", "ticker", "symbol", "code")
        if ticker is None:
            continue
        ticker = ticker.upper()
        if _TICKER_PATTERN.fullmatch(ticker) is None or ticker in seen:
            continue
        seen.add(ticker)
        raw_type = _text(row, "type", "assetType", "asset_type", "category")
        name = _text(row, "name", "longName", "companyName", "company_name", "description")
        instrument_type = _directory_instrument_type(raw_type, name, ticker)
        exchange = _text(row, "exchange", "market", "exchangeName") or "B3"
        country = _text(row, "country", "countryCode") or "BR"
        isin = _text(row, "isin", "ISIN")
        underlying_ticker = _text(
            row,
            "underlyingTicker",
            "underlying_ticker",
            "underlyingSymbol",
            "referenceTicker",
        )
        identifiers = {"isin": isin} if isin else {}
        underlying_name = _text(row, "underlyingName", "underlying_name")
        result.append(
            InstrumentMetadata(
                ticker=ticker,
                name=name,
                instrument_type=instrument_type,
                category=raw_type,
                isin=isin,
                identifiers=identifiers,
                exchange=exchange.upper(),
                country=country.upper(),
                underlying_ticker=(underlying_ticker.upper() if underlying_ticker else None),
                underlying_name=underlying_name,
                underlying_exchange=(
                    _text(row, "underlyingExchange", "underlying_exchange") or None
                ),
                underlying_country=(_text(row, "underlyingCountry", "underlying_country") or None),
                underlying_source=SOURCE_BRAPI_DIRECTORY if underlying_ticker else None,
                source=SOURCE_BRAPI_DIRECTORY,
                confidence="medium",
            )
        )
    return result


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("stocks", "results", "data", "instruments", "assets"):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    rows: list[dict[str, Any]] = []
    for value in payload.values():
        if isinstance(value, list):
            rows.extend(row for row in value if isinstance(row, dict))
    return rows


def _text(row: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _directory_instrument_type(
    raw_type: str | None,
    name: str | None,
    ticker: str,
) -> InstrumentType:
    text = " ".join(value for value in (raw_type, name) if value).upper()
    if "BDR" in text or "DEPOSITARY" in text:
        return InstrumentType.bdr
    if "ETF" in text or "FUNDO DE IND" in text:
        return InstrumentType.etf
    if "FIAGRO" in text or "FI AGRO" in text:
        return InstrumentType.fiagro
    if "FI-INFRA" in text or "FI INFRA" in text:
        return InstrumentType.fi_infra
    if "FII" in text or "IMOBILI" in text or "REAL ESTATE" in text:
        return InstrumentType.fii
    if (
        raw_type
        and raw_type.strip().upper() in {"FUND", "FUNDS", "FUNDOS"}
        and ticker.endswith("11")
    ):
        return InstrumentType.fii
    if "UNIT" in text or " UNT" in f" {text}":
        return InstrumentType.unit
    if "STOCK" in text or "EQUITY" in text or "COMMON" in text:
        return InstrumentType.stock
    # A suffix alone is not authoritative: several B3 classes share suffixes
    # and upstream list rows occasionally omit their type. Preserve that
    # uncertainty instead of inventing a security class.
    return InstrumentType.unknown


__all__ = [
    "BrapiInstrumentDirectoryProvider",
    "SOURCE_BRAPI_DIRECTORY",
    "parse_brapi_directory",
]
