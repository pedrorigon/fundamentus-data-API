"""Free B3 BDI fallback for fixed-income instruments traded on the exchange."""

from __future__ import annotations

import asyncio
import base64
from datetime import date
from decimal import Decimal, InvalidOperation
from time import monotonic
from typing import Any

import httpx

from app.config import Settings

SOURCE_B3_BDI = "b3_bdi_consolidated"
_TABLE_NAME = "ConsolidatedRecords"
_CACHE_TTL_SECONDS = 3600.0


class B3FixedIncomeProvider:
    """Resolve B3 reference prices for instruments with a consolidated trade.

    The BDI endpoint is public and contains individual instruments only when a
    trade was consolidated for the requested date.  A missing row is therefore
    returned as unavailable; no curve or contractual accrual is inferred.
    """

    source = SOURCE_B3_BDI
    identifier_scoped = True

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self._cache: dict[tuple[date, str], tuple[float, Decimal | None]] = {}
        self._semaphore = asyncio.Semaphore(max(1, self.settings.upstream_concurrency))

    async def prices(self, _reference: date) -> dict[str, Decimal]:
        # The BDI table is intentionally queried by identifier through
        # ``prices_for``.  Calling the broad method cannot safely enumerate
        # thousands of instruments, so it reports no values.
        return {}

    async def prices_for(self, reference: date, identifiers: set[str]) -> dict[str, Decimal]:
        normalized = {value.strip().upper() for value in identifiers if value.strip()}
        if not normalized or not self.settings.b3_bdi_base_url:
            return {}
        result: dict[str, Decimal] = {}
        missing: list[str] = []
        now = monotonic()
        for identifier in normalized:
            cached = self._cache.get((reference, identifier))
            if cached is None or cached[0] <= now:
                missing.append(identifier)
            elif cached[1] is not None:
                result[identifier] = cached[1]
        if not missing:
            return result

        timeout = httpx.Timeout(self.settings.request_timeout_seconds)
        async with httpx.AsyncClient(
            base_url=self.settings.b3_bdi_base_url.rstrip("/"),
            timeout=timeout,
            transport=self.transport,
            headers={
                "Accept": "application/json",
                "User-Agent": self.settings.user_agent,
            },
        ) as client:

            async def fetch(identifier: str) -> tuple[str, Decimal | None]:
                async with self._semaphore:
                    try:
                        response = await client.post(
                            f"/table/{_TABLE_NAME}/{reference}/{reference}/1/20",
                            params={"filter": _encode_filter(identifier)},
                            json={},
                        )
                        if response.status_code == 404:
                            return identifier, None
                        response.raise_for_status()
                        prices = parse_b3_reference_prices(response.json())
                        return identifier, prices.get(identifier)
                    except (httpx.HTTPError, TypeError, ValueError):
                        return identifier, None

            values = await asyncio.gather(*(fetch(identifier) for identifier in missing))
        expires_at = monotonic() + _CACHE_TTL_SECONDS
        for identifier, price in values:
            self._cache[(reference, identifier)] = (expires_at, price)
            if price is not None:
                result[identifier] = price
        return result


def parse_b3_reference_prices(payload: object) -> dict[str, Decimal]:
    """Extract one positive BDI reference price per consolidated instrument."""

    if not isinstance(payload, dict):
        return {}
    table = payload.get("table")
    if not isinstance(table, dict) or not isinstance(table.get("values"), list):
        return {}
    result: dict[str, Decimal] = {}
    for raw_row in table["values"]:
        if not isinstance(raw_row, list) or len(raw_row) <= 12:
            continue
        identifier = raw_row[2] if isinstance(raw_row[2], str) else ""
        if not identifier:
            continue
        for value in (raw_row[12], raw_row[11], raw_row[9]):
            price = _positive_decimal(value)
            if price is not None:
                result[identifier.upper()] = price
                break
    return result


def _encode_filter(identifier: str) -> str:
    return base64.b64encode(identifier.encode("ascii")).decode("ascii")


def _positive_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        if isinstance(value, str):
            normalized = value.strip().replace(".", "", 1) if "," in value else value.strip()
            parsed = Decimal(normalized.replace(",", "."))
        else:
            parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None
