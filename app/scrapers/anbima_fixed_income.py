from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal, InvalidOperation
from io import StringIO

import httpx

from app.config import Settings

SOURCE_ANBIMA = "anbima"
MISSING_VALUE = "--"


class AnbimaDebentureProvider:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport

    async def prices(self, reference: date) -> dict[str, Decimal]:
        filename = f"db{reference:%y%m%d}.txt"
        async with httpx.AsyncClient(
            base_url=self.settings.anbima_debenture_base_url,
            timeout=httpx.Timeout(self.settings.request_timeout_seconds),
            transport=self.transport,
            headers={"User-Agent": self.settings.user_agent},
        ) as client:
            response = await client.get(f"/{filename}")
        if response.status_code == 404:
            return {}
        response.raise_for_status()
        return parse_anbima_debenture_prices(response.content)


def parse_anbima_debenture_prices(payload: bytes) -> dict[str, Decimal]:
    text = payload.decode("latin-1")
    header_index = text.find("Código@")
    if header_index < 0:
        return {}
    rows = csv.DictReader(StringIO(text[header_index:]), delimiter="@")
    prices: dict[str, Decimal] = {}
    for row in rows:
        identifier = (row.get("Código") or "").strip().upper()
        unit_price = _decimal(row.get("PU"))
        if identifier and unit_price is not None and unit_price > 0:
            prices[identifier] = unit_price
    return prices


def _decimal(value: str | None) -> Decimal | None:
    normalized = (value or "").strip()
    if not normalized or normalized == MISSING_VALUE:
        return None
    try:
        return Decimal(normalized.replace(".", "").replace(",", "."))
    except InvalidOperation:
        return None
