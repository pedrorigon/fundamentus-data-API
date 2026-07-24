"""Download CVM open-data statement archives.

CVM publishes one ZIP archive per document type and year. Archives are large, so
callers are expected to cache the parsed result rather than the raw payload.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import httpx

from app.config import Settings
from app.parsers.cvm_statements import (
    CompanyRegistration,
    ShareCapital,
    StatementPeriod,
    parse_company_registry,
    parse_share_capital,
    parse_statement_archive,
)


class StatementKind(StrEnum):
    ANNUAL = "dfp"
    QUARTERLY = "itr"


@dataclass(frozen=True)
class StatementArchive:
    kind: StatementKind
    year: int
    periods: dict[str, list[StatementPeriod]]
    share_capital: dict[str, ShareCapital]


class CvmOpenDataProvider:
    """Fetch and parse CVM statement archives and the company registry."""

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport

    async def statements(
        self,
        kind: StatementKind,
        year: int,
        cnpjs: set[str] | None = None,
    ) -> StatementArchive | None:
        payload = await self._download(
            f"/dados/CIA_ABERTA/DOC/{kind.value.upper()}/DADOS/{kind.value}_cia_aberta_{year}.zip"
        )
        if payload is None:
            return None
        return StatementArchive(
            kind=kind,
            year=year,
            periods=parse_statement_archive(payload, cnpjs=cnpjs),
            share_capital=parse_share_capital(payload, cnpjs=cnpjs),
        )

    async def registry(self) -> dict[str, CompanyRegistration]:
        payload = await self._download("/dados/CIA_ABERTA/CAD/DADOS/cad_cia_aberta.csv")
        if payload is None:
            return {}
        return parse_company_registry(payload)

    async def _download(self, path: str) -> bytes | None:
        timeout = httpx.Timeout(self.settings.cvm_request_timeout_seconds)
        try:
            async with httpx.AsyncClient(
                base_url=self.settings.cvm_open_data_base_url,
                timeout=timeout,
                transport=self.transport,
                follow_redirects=True,
                headers={"User-Agent": self.settings.user_agent},
            ) as client:
                response = await client.get(path)
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                return response.content
        except httpx.HTTPError:
            return None
