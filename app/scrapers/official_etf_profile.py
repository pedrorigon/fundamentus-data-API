"""Exact-identity ETF facts from the issuer's public fund portal.

The registry records only documented listings. An unknown listing or a manager
response with a different ticker/ISIN is never joined to the fund profile.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from io import BytesIO
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile, ZipFile

import httpx
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from app.config import Settings
from app.core.archive_safety import ArchiveSafetyError, read_bounded_body
from app.models import FundHolding, FundProfile, InstrumentMetadata, InstrumentType

_MANAGER_API = "https://www.btgpactual.com/etf/api"
_MAX_PROFILE_BYTES = 512_000
_MAX_HOLDINGS_BYTES = 128_000
_MAX_HOLDINGS_EXPANDED_BYTES = 2_000_000
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
            holdings, holdings_date = await self._holdings(client, verified, reference)
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
            holdings=holdings,
            holdings_date=holdings_date,
            holdings_source=(
                f"{self.base_url}/Composicao/DownloadCarteira/?ID={verified.manager_id}"
                if holdings
                else None
            ),
            holdings_grouped_by_label=bool(holdings),
            source=verified.source_url,
        )

    async def _holdings(
        self,
        client: httpx.AsyncClient,
        verified: VerifiedEtf,
        reference: date,
    ) -> tuple[list[FundHolding], date | None]:
        try:
            async with client.stream(
                "GET", "/Composicao/DownloadCarteira/", params={"ID": verified.manager_id}
            ) as response:
                response.raise_for_status()
                if "spreadsheetml.sheet" not in response.headers.get("content-type", ""):
                    return [], None
                content = await read_bounded_body(response, _MAX_HOLDINGS_BYTES)
        except (httpx.HTTPError, ArchiveSafetyError):
            return [], None
        return _parse_holdings(content, reference)

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


def _parse_holdings(content: bytes, reference: date) -> tuple[list[FundHolding], date | None]:
    """Group manager portfolio lots by disclosed asset label, never by row count."""
    try:
        with ZipFile(BytesIO(content)) as archive:
            members = archive.infolist()
            if (
                len(members) > 40
                or sum(member.file_size for member in members) > _MAX_HOLDINGS_EXPANDED_BYTES
                or any(member.file_size > max(1, member.compress_size) * 100 for member in members)
            ):
                return [], None
        workbook = load_workbook(BytesIO(content), read_only=True, data_only=True)
    except (BadZipFile, InvalidFileException, KeyError, OSError, ParseError, ValueError):
        return [], None
    try:
        if "Carteira" not in workbook.sheetnames:
            return [], None
        rows = workbook["Carteira"].iter_rows(values_only=True)
        for index, header in enumerate(rows):
            if index >= 20:
                return [], None
            normalized = tuple(str(value or "").strip().upper() for value in header)
            if normalized[:7] == (
                "DATA",
                "CATEGORIA",
                "ATIVO",
                "QUANTIDADE",
                "PREÇO (R$)",
                "FINANCEIRO (R$)",
                "PESO (%)",
            ):
                break
        else:
            return [], None
        grouped: dict[str, Decimal] = defaultdict(Decimal)
        total_financial = Decimal("0")
        total_weight = Decimal("0")
        as_of: date | None = None
        for index, row in enumerate(rows):
            if index >= 500:
                return [], None
            if not any(value is not None for value in row):
                continue
            if len(row) < 7 or not isinstance(row[0], (date, datetime)):
                return [], None
            observed_at = row[0].date() if isinstance(row[0], datetime) else row[0]
            if not 0 <= (reference - observed_at).days <= _MAX_ASSET_AGE_DAYS:
                return [], None
            if as_of is not None and observed_at != as_of:
                return [], None
            as_of = observed_at
            label = str(row[2] or "").strip().upper()
            financial = _finite_decimal(row[5])
            weight = _finite_decimal(row[6])
            if not label or financial is None or weight is None:
                return [], None
            total_financial += financial
            total_weight += weight
            if financial > 0 and not label.startswith(("PROV.", "DESPESA", "TAXA")):
                grouped[label] += financial
        invested = sum(grouped.values(), Decimal("0"))
        if (
            as_of is None
            or invested <= 0
            or total_financial <= 0
            or abs(total_weight - Decimal("100")) > Decimal("0.5")
            or abs(invested - total_financial) / total_financial > Decimal("0.005")
        ):
            return [], None
        holdings = [
            FundHolding(symbol=label, weight=financial / invested)
            for label, financial in sorted(grouped.items(), key=lambda item: (-item[1], item[0]))
        ]
        return holdings, as_of
    finally:
        workbook.close()


def _finite_decimal(value: object) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None
