"""Exact-identity ETF facts from the issuer's public fund portal.

The registry records only documented listings. An unknown listing or a manager
response with a different ticker/ISIN is never joined to the fund profile.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation

import httpx

from app.config import Settings
from app.core.archive_safety import ArchiveSafetyError, read_bounded_body
from app.models import FundProfile, InstrumentMetadata, InstrumentType

_MANAGER_API = "https://www.btgpactual.com/etf/api"
_MAX_PROFILE_BYTES = 512_000
_MAX_ASSET_AGE_DAYS = 7
_FEE_PATTERN = re.compile(r"^([0-9]+(?:[.,][0-9]+)?)%\s*a\.a\.$", re.IGNORECASE)


@dataclass(frozen=True)
class VerifiedEtf:
    ticker: str
    isin: str
    manager_id: int
    source_url: str


# The exchange commencement notice binds ABTC11 to this ISIN. The manager
# portal's own instrument id is 126 and its Characteristics endpoint must
# repeat both identifiers before any observed fee or asset value is admitted.
# https://fnet.bmfbovespa.com.br/fnet/publico/exibirDocumento?id=1241682
_VERIFIED_ETFS = {
    ("ABTC11", "BRABTCCTF002"): VerifiedEtf(
        ticker="ABTC11",
        isin="BRABTCCTF002",
        manager_id=126,
        source_url="https://www.btgpactual.com/asset-management/etf/ABTC11",
    ),
}


class OfficialEtfProfileProvider:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        base_url: str = _MANAGER_API,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self.base_url = base_url

    async def get(
        self,
        instrument: InstrumentMetadata,
        *,
        today: date | None = None,
    ) -> FundProfile | None:
        if (
            instrument.instrument_type is not InstrumentType.etf
            or instrument.source != "b3"
            or instrument.confidence not in {"high", "verified", "authoritative"}
        ):
            return None
        key = (instrument.ticker.upper(), (instrument.isin or "").upper())
        verified = _VERIFIED_ETFS.get(key)
        if verified is None:
            return None
        reference = today or datetime.now(UTC).date()
        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(self.settings.request_timeout_seconds),
            transport=self.transport,
            follow_redirects=True,
        ) as client:
            characteristics, daily = await asyncio.gather(
                self._payload(client, "/Caracteristica/", {"ID": verified.manager_id}),
                self._payload(
                    client,
                    "/Rentabilidade/GetEvolucaoDiaria/",
                    {"ID": verified.manager_id, "ano": reference.year, "mes": reference.month},
                ),
            )
            net_assets, net_assets_date = _latest_assets(daily, reference)
            if net_assets is None and reference.day <= _MAX_ASSET_AGE_DAYS:
                previous_month = reference.replace(day=1) - timedelta(days=1)
                previous_daily = await self._payload(
                    client,
                    "/Rentabilidade/GetEvolucaoDiaria/",
                    {
                        "ID": verified.manager_id,
                        "ano": previous_month.year,
                        "mes": previous_month.month,
                    },
                )
                net_assets, net_assets_date = _latest_assets(previous_daily, reference)
        details = _result(characteristics)
        if (
            details is None
            or str(details.get("Ticker") or "").upper() != verified.ticker
            or str(details.get("CodISINFundo") or "").upper() != verified.isin
        ):
            return None
        fee = _annual_fee(details.get("TaxaAdministracao"))
        inception = _portal_date(details.get("DataInicio"))
        if fee is None and net_assets is None:
            return None
        return FundProfile(
            net_assets=net_assets,
            net_assets_date=net_assets_date,
            net_assets_source=verified.source_url if net_assets is not None else None,
            net_expense_ratio=fee,
            inception_date=inception,
            description=str(details.get("IndiceReferencia") or ""),
            source=verified.source_url,
        )

    async def _payload(
        self,
        client: httpx.AsyncClient,
        path: str,
        params: dict[str, int],
    ) -> dict[str, object] | None:
        try:
            async with client.stream("GET", path, params=params) as response:
                response.raise_for_status()
                content = await read_bounded_body(response, _MAX_PROFILE_BYTES)
            payload = json.loads(content)
            return payload if isinstance(payload, dict) else None
        except (httpx.HTTPError, ArchiveSafetyError, ValueError):
            return None


def _result(payload: dict[str, object] | None) -> dict[str, object] | None:
    if payload is None or payload.get("ErrorWarn") is not False:
        return None
    result = payload.get("ObjJsonResultado")
    return result if isinstance(result, dict) else None


def _annual_fee(value: object) -> Decimal | None:
    match = _FEE_PATTERN.fullmatch(str(value or "").strip())
    if match is None:
        return None
    ratio = Decimal(match.group(1).replace(",", ".")) / Decimal("100")
    return ratio if Decimal("0") <= ratio <= Decimal("0.10") else None


def _portal_date(value: object) -> date | None:
    text = str(value or "")[:10]
    try:
        return datetime.strptime(text, "%d/%m/%Y").date()
    except ValueError:
        return None


def _latest_assets(
    payload: dict[str, object] | None, reference: date
) -> tuple[Decimal | None, date | None]:
    result = _result(payload)
    rows = result.get("EvolucaoDiaria") if result is not None else None
    if not isinstance(rows, list):
        return None, None
    observations: list[tuple[date, Decimal]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        as_of = _portal_date(row.get("Data"))
        try:
            assets = Decimal(str(row.get("PatrimonioLiquido")))
        except InvalidOperation:
            continue
        if (
            as_of is not None
            and assets.is_finite()
            and assets > 0
            and 0 <= (reference - as_of).days <= _MAX_ASSET_AGE_DAYS
        ):
            observations.append((as_of, assets))
    if not observations:
        return None, None
    latest_date, latest_assets = max(observations, key=lambda item: item[0])
    return latest_assets, latest_date
