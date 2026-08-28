from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
import io
import json
import re
import time
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Protocol

import httpx
from pypdf import PdfReader

from app.config import Settings
from app.core.archive_safety import open_validated_zip, read_bounded_body
from app.income.parsers import (
    parse_b3_income_events,
    parse_cvm_income_adjustment_text,
    parse_cvm_income_report_text,
    parse_fundos_net_xml,
    parse_status_invest_income_events,
)
from app.models import Dividend
from app.models.income_events import (
    IncomeEventObservation,
    IncomeInstrumentRequest,
    IncomeSourceCoverage,
)
from app.parsers.status_invest import parse_status_invest_cnpj
from app.services.assets import AssetService


@dataclass(frozen=True)
class IncomeSourceResult:
    observations: list[IncomeEventObservation]
    coverage: list[IncomeSourceCoverage]


class IncomeSource(Protocol):
    name: str
    snapshot_sources: tuple[str, ...]

    async def collect(
        self,
        instruments: Sequence[IncomeInstrumentRequest],
        as_of: date,
    ) -> IncomeSourceResult: ...


class FundamentusIncomeSource:
    name = "fundamentus"
    snapshot_sources: tuple[str, ...] = ("fundamentus",)

    def __init__(self, assets: AssetService) -> None:
        self.assets = assets

    async def collect(
        self,
        instruments: Sequence[IncomeInstrumentRequest],
        as_of: date,
    ) -> IncomeSourceResult:
        del as_of
        results = await asyncio.gather(
            *(self._instrument(item) for item in instruments),
            return_exceptions=True,
        )
        return _instrument_results(self.name, instruments, results)

    async def _instrument(
        self, instrument: IncomeInstrumentRequest
    ) -> list[IncomeEventObservation]:
        dividends, _cached = await self.assets.get_dividends(
            instrument.ticker,
            force_refresh=True,
        )
        return [
            _fundamentus_observation(instrument, item, ordinal)
            for ordinal, item in enumerate(dividends)
            if _usable_dividend(item)
        ]


class StatusInvestIncomeSource:
    name = "status_invest"
    snapshot_sources: tuple[str, ...] = ("status_invest",)

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self._profiles: dict[str, tuple[float, str]] = {}
        self._profile_tasks: dict[str, asyncio.Task[str]] = {}
        self._profile_lock = asyncio.Lock()

    async def collect(
        self,
        instruments: Sequence[IncomeInstrumentRequest],
        as_of: date,
    ) -> IncomeSourceResult:
        del as_of
        async with self._client() as client:
            results = await asyncio.gather(
                *(self._instrument(client, item) for item in instruments),
                return_exceptions=True,
            )
        return _instrument_results(self.name, instruments, results)

    async def fund_cnpjs(
        self,
        instruments: Sequence[IncomeInstrumentRequest],
    ) -> dict[str, str]:
        funds = [item for item in instruments if item.ticker.upper().endswith("11")]
        if not funds:
            return {}
        async with self._client() as client:
            pages = await asyncio.gather(
                *(self._profile_html(client, item) for item in funds),
                return_exceptions=True,
            )
        return {
            instrument.ticker: cnpj
            for instrument, page in zip(funds, pages, strict=True)
            if isinstance(page, str) and (cnpj := parse_status_invest_cnpj(page)) is not None
        }

    async def _instrument(
        self,
        client: httpx.AsyncClient,
        instrument: IncomeInstrumentRequest,
    ) -> list[IncomeEventObservation]:
        html = await self._profile_html(client, instrument)
        return parse_status_invest_income_events(html, ticker=instrument.ticker) if html else []

    async def _profile_html(
        self,
        client: httpx.AsyncClient,
        instrument: IncomeInstrumentRequest,
    ) -> str:
        ticker = instrument.ticker.upper()
        async with self._profile_lock:
            cached = self._profiles.get(ticker)
            if cached is not None and _fresh(
                cached[0], self.settings.income_source_index_ttl_seconds
            ):
                return cached[1]
            task = self._profile_tasks.get(ticker)
            if task is None:
                task = asyncio.create_task(self._fetch_profile_html(client, instrument))
                self._profile_tasks[ticker] = task
        try:
            html = await task
            async with self._profile_lock:
                self._profiles[ticker] = (time.monotonic(), html)
                _trim_ttl_cache(self._profiles, self.settings.ticker_cache_max_entries)
            return html
        finally:
            async with self._profile_lock:
                if self._profile_tasks.get(ticker) is task:
                    self._profile_tasks.pop(ticker, None)

    async def _fetch_profile_html(
        self,
        client: httpx.AsyncClient,
        instrument: IncomeInstrumentRequest,
    ) -> str:
        for asset_type in ("fundos-imobiliarios", "acoes"):
            response = await client.get(f"/{asset_type}/{instrument.ticker.lower()}")
            if response.status_code == 404:
                continue
            response.raise_for_status()
            return response.text
        return ""

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.settings.status_invest_base_url,
            timeout=httpx.Timeout(self.settings.request_timeout_seconds),
            limits=_limits(self.settings),
            follow_redirects=True,
            headers={
                "Accept": "text/html",
                "Accept-Language": "pt-BR,pt;q=0.9",
                "User-Agent": self.settings.user_agent,
            },
            transport=self.transport,
        )


class OfficialCompanyIncomeSource:
    name = "official_companies"
    snapshot_sources: tuple[str, ...] = ("b3", "cvm")

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self._cvm_index: dict[int, tuple[float, list[dict[str, str]]]] = {}
        self._cvm_lock = asyncio.Lock()
        self._cvm_documents: dict[str, tuple[float, str]] = {}
        self._cvm_document_tasks: dict[str, asyncio.Task[str]] = {}
        self._cvm_document_lock = asyncio.Lock()
        self._cvm_download_semaphore = asyncio.Semaphore(
            min(max(settings.upstream_concurrency, 1), 2)
        )

    async def collect(
        self,
        instruments: Sequence[IncomeInstrumentRequest],
        as_of: date,
    ) -> IncomeSourceResult:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.settings.cvm_request_timeout_seconds),
            limits=_limits(self.settings),
            follow_redirects=True,
            headers={
                "Accept": "application/json,application/pdf",
                "User-Agent": self.settings.user_agent,
            },
            transport=self.transport,
        ) as client:
            results = await asyncio.gather(
                *(self._instrument(client, item, as_of) for item in instruments),
                return_exceptions=True,
            )
        return _instrument_results(self.name, instruments, results)

    async def _instrument(
        self,
        client: httpx.AsyncClient,
        instrument: IncomeInstrumentRequest,
        as_of: date,
    ) -> list[IncomeEventObservation]:
        b3_payload = await self._b3_payload(client, instrument.ticker)
        if instrument.isin is None and _distinct_b3_isins(b3_payload) > 1:
            raise ValueError(f"B3 returned multiple ISINs for {instrument.ticker}")
        b3_events, cvm_code = parse_b3_income_events(
            b3_payload,
            ticker=instrument.ticker,
            requested_isin=instrument.isin,
        )
        if cvm_code is None:
            return b3_events
        cvm_events = await self._cvm_events(client, instrument, cvm_code, as_of)
        return [*b3_events, *cvm_events]

    async def _b3_payload(self, client: httpx.AsyncClient, ticker: str) -> Any:
        issuer = _issuer_code(ticker)
        encoded = base64.b64encode(
            json.dumps(
                {"language": "pt-br", "issuingCompany": issuer}, separators=(",", ":")
            ).encode()
        ).decode()
        endpoint = f"/CompanyCall/GetListedSupplementCompany/{encoded}"
        url = f"{self.settings.b3_listed_companies_base_url.rstrip('/')}{endpoint}"
        response = await client.get(url)
        if response.status_code == 404:
            return []
        response.raise_for_status()
        payload = response.json()
        return json.loads(payload) if isinstance(payload, str) else payload

    async def _cvm_events(
        self,
        client: httpx.AsyncClient,
        instrument: IncomeInstrumentRequest,
        cvm_code: str,
        as_of: date,
    ) -> list[IncomeEventObservation]:
        years = list(dict.fromkeys((as_of.year, as_of.year - 1)))
        rows: list[dict[str, str]] = []
        for year in years:
            rows.extend(await self._cvm_rows(client, year))
        company_rows = [
            row for row in rows if row.get("Codigo_CVM", "").lstrip("0") == cvm_code.lstrip("0")
        ]
        reports = [
            row for row in company_rows if _fold(row.get("Categoria", "")) == "RELATORIO PROVENTOS"
        ]
        adjustments = [row for row in company_rows if _cvm_income_adjustment(row)]
        latest = [
            *_latest_cvm_documents(reports, limit=24),
            *_latest_cvm_documents(adjustments, limit=12),
        ]
        results = await asyncio.gather(
            *(self._cvm_document(client, instrument, row) for row in latest),
            return_exceptions=True,
        )
        failure = next((result for result in results if isinstance(result, BaseException)), None)
        if failure is not None:
            raise RuntimeError("CVM income document refresh was incomplete") from failure
        return [event for result in results if isinstance(result, list) for event in result]

    async def _cvm_rows(self, client: httpx.AsyncClient, year: int) -> list[dict[str, str]]:
        async with self._cvm_lock:
            cached = self._cvm_index.get(year)
            if cached is not None and _fresh(
                cached[0], self.settings.income_source_index_ttl_seconds
            ):
                return cached[1]
            url = (
                f"{self.settings.cvm_open_data_base_url.rstrip('/')}/dados/CIA_ABERTA/"
                f"DOC/IPE/DADOS/ipe_cia_aberta_{year}.zip"
            )
            response = await client.get(url)
            if response.status_code == 404:
                self._cvm_index[year] = (time.monotonic(), [])
                return []
            response.raise_for_status()
            payload = await read_bounded_body(response, self.settings.archive_download_max_bytes)
            rows = _read_cvm_zip(payload)
            self._cvm_index[year] = (time.monotonic(), rows)
            return rows

    async def _cvm_document(
        self,
        client: httpx.AsyncClient,
        instrument: IncomeInstrumentRequest,
        row: dict[str, str],
    ) -> list[IncomeEventObservation]:
        link = row.get("Link_Download", "").strip()
        if not link:
            return []
        text = await self._cvm_document_text(client, link)
        parser = (
            parse_cvm_income_adjustment_text
            if _cvm_income_adjustment(row)
            else parse_cvm_income_report_text
        )
        events = parser(
            text,
            ticker=instrument.ticker,
            document_id=_cvm_document_id(row),
            version=_positive_int(row.get("Versao")),
        )
        if instrument.isin:
            return [item for item in events if item.isin in {None, instrument.isin}]
        return events

    async def _cvm_document_text(self, client: httpx.AsyncClient, link: str) -> str:
        async with self._cvm_document_lock:
            cached = self._cvm_documents.get(link)
            if cached is not None and _fresh(
                cached[0], self.settings.income_source_index_ttl_seconds
            ):
                return cached[1]
            task = self._cvm_document_tasks.get(link)
            if task is None:
                task = asyncio.create_task(self._fetch_cvm_document_text(client, link))
                self._cvm_document_tasks[link] = task
        try:
            text = await task
            async with self._cvm_document_lock:
                self._cvm_documents[link] = (time.monotonic(), text)
                _trim_ttl_cache(
                    self._cvm_documents,
                    self.settings.ticker_cache_max_entries,
                )
            return text
        finally:
            async with self._cvm_document_lock:
                if self._cvm_document_tasks.get(link) is task:
                    self._cvm_document_tasks.pop(link, None)

    async def _fetch_cvm_document_text(self, client: httpx.AsyncClient, link: str) -> str:
        async with self._cvm_download_semaphore:
            response = await client.get(
                link,
                headers={"Referer": "https://www.rad.cvm.gov.br/ENET/frmConsultaExternaCVM.aspx"},
            )
            response.raise_for_status()
            content = await read_bounded_body(
                response,
                self.settings.income_document_max_bytes,
            )
        return _pdf_text(content)


class FundosNetIncomeSource:
    name = "fundos_net"
    snapshot_sources: tuple[str, ...] = ("fundos_net",)

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
        status_source: StatusInvestIncomeSource | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self.status_source = status_source
        self._index: tuple[float, list[dict[str, Any]]] | None = None
        self._index_lock = asyncio.Lock()
        self._documents: dict[tuple[str, int], tuple[float, list[IncomeEventObservation]]] = {}
        self._document_tasks: dict[tuple[str, int], asyncio.Task[list[IncomeEventObservation]]] = {}
        self._document_lock = asyncio.Lock()
        self._fund_rows: dict[
            tuple[str, date],
            tuple[float, tuple[list[dict[str, Any]], bool]],
        ] = {}
        self._fund_row_tasks: dict[
            tuple[str, date],
            asyncio.Task[tuple[list[dict[str, Any]], bool]],
        ] = {}
        self._fund_row_lock = asyncio.Lock()

    async def collect(
        self,
        instruments: Sequence[IncomeInstrumentRequest],
        as_of: date,
    ) -> IncomeSourceResult:
        requested = {item.ticker: item for item in instruments}
        try:
            observations, complete_tickers = await self._load(requested, as_of)
        except (httpx.HTTPError, ValueError) as exc:
            failed_coverage = [
                _coverage(self.name, ticker, "failed", False, str(exc)) for ticker in requested
            ]
            return IncomeSourceResult([], failed_coverage)
        found = {item.ticker for item in observations}
        coverage: list[IncomeSourceCoverage] = []
        for ticker in requested:
            complete = ticker in complete_tickers
            status = (
                "complete" if complete and ticker in found else "empty" if complete else "partial"
            )
            coverage.append(
                _coverage(
                    self.name,
                    ticker,
                    status,
                    complete,
                    None if complete else "fund-specific snapshot unavailable",
                )
            )
        return IncomeSourceResult(observations, coverage)

    async def _load(
        self,
        requested: dict[str, IncomeInstrumentRequest],
        as_of: date,
    ) -> tuple[list[IncomeEventObservation], set[str]]:
        headers = {
            "Accept": "application/json,application/xml,text/xml",
            "User-Agent": self.settings.user_agent,
        }
        async with httpx.AsyncClient(
            base_url=self.settings.fundos_net_base_url,
            timeout=httpx.Timeout(self.settings.cvm_request_timeout_seconds),
            limits=_limits(self.settings),
            follow_redirects=True,
            headers=headers,
            transport=self.transport,
        ) as client:
            rows = await self._rows(client)
            targeted_candidates: list[dict[str, Any]] = []
            complete_tickers: set[str] = set()
            if self.status_source is not None:
                cnpjs = await self.status_source.fund_cnpjs(list(requested.values()))
                targeted = await asyncio.gather(
                    *(
                        self._rows_for_cnpj(
                            client,
                            cnpj,
                            as_of - timedelta(days=self.settings.income_snapshot_overlap_days),
                        )
                        for ticker, cnpj in cnpjs.items()
                    ),
                    return_exceptions=True,
                )
                for (ticker, _cnpj), result in zip(cnpjs.items(), targeted, strict=True):
                    if isinstance(result, BaseException):
                        continue
                    targeted_rows, complete = result
                    targeted_candidates.extend(targeted_rows)
                    if complete:
                        complete_tickers.add(ticker)
            candidates = list(
                {
                    str(row["id"]): row
                    for row in [
                        *targeted_candidates,
                        *_fundos_net_candidates(
                            rows,
                            requested,
                            self.settings.fundos_net_fallback_documents,
                        ),
                    ]
                }.values()
            )
            semaphore = asyncio.Semaphore(self.settings.upstream_concurrency)

            async def download(row: dict[str, Any]) -> list[IncomeEventObservation]:
                async with semaphore:
                    return await self._document(client, row)

            parsed = await asyncio.gather(
                *(download(row) for row in candidates), return_exceptions=True
            )
            failure = next((result for result in parsed if isinstance(result, BaseException)), None)
            if failure is not None:
                raise RuntimeError("Fundos.NET document refresh was incomplete") from failure
        observations = [
            event
            for result in parsed
            if isinstance(result, list)
            for event in result
            if event.ticker in requested
        ]
        return observations, complete_tickers

    async def _rows(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        async with self._index_lock:
            if self._index is not None and _fresh(
                self._index[0], self.settings.income_source_index_ttl_seconds
            ):
                return self._index[1]
            response = await client.get(
                "/fnet/publico/pesquisarGerenciadorDocumentosDados",
                params={
                    "d": 1,
                    "s": 0,
                    "l": self.settings.fundos_net_scan_limit,
                    "o[0][dataEntrega]": "desc",
                    "idCategoriaDocumento": 14,
                    "idTipoDocumento": 41,
                    "tipoFundo": 1,
                },
            )
            response.raise_for_status()
            rows = _fundos_net_rows(response.json())
            self._index = (time.monotonic(), rows)
            return rows

    async def _rows_for_cnpj(
        self,
        client: httpx.AsyncClient,
        cnpj: str,
        snapshot_from: date,
    ) -> tuple[list[dict[str, Any]], bool]:
        key = (cnpj, snapshot_from)
        async with self._fund_row_lock:
            cached = self._fund_rows.get(key)
            if cached is not None and _fresh(
                cached[0], self.settings.income_source_index_ttl_seconds
            ):
                return cached[1]
            task = self._fund_row_tasks.get(key)
            if task is None:
                task = asyncio.create_task(self._fetch_rows_for_cnpj(client, cnpj, snapshot_from))
                self._fund_row_tasks[key] = task
        try:
            result = await task
            async with self._fund_row_lock:
                self._fund_rows[key] = (time.monotonic(), result)
                _trim_ttl_cache(self._fund_rows, self.settings.ticker_cache_max_entries)
            return result
        finally:
            async with self._fund_row_lock:
                if self._fund_row_tasks.get(key) is task:
                    self._fund_row_tasks.pop(key, None)

    async def _fetch_rows_for_cnpj(
        self,
        client: httpx.AsyncClient,
        cnpj: str,
        snapshot_from: date,
    ) -> tuple[list[dict[str, Any]], bool]:
        rows: list[dict[str, Any]] = []
        page_size = max(self.settings.fundos_net_page_size, 1)
        scan_limit = max(self.settings.fundos_net_scan_limit, page_size)
        for offset in range(0, scan_limit, page_size):
            response = await client.get(
                "/fnet/publico/pesquisarGerenciadorDocumentosDados",
                params={
                    "d": 1,
                    "s": offset,
                    "l": min(page_size, scan_limit - offset),
                    "o[0][dataEntrega]": "desc",
                    "idCategoriaDocumento": 14,
                    "idTipoDocumento": 41,
                    "idEspecieDocumento": 0,
                    "tipoFundo": 1,
                    "cnpj": cnpj,
                    "cnpjFundo": cnpj,
                },
            )
            response.raise_for_status()
            payload = response.json()
            page = _fundos_net_rows(payload)
            rows.extend(page)
            total = _positive_int(payload.get("recordsFiltered"))
            exhausted = not page or offset + len(page) >= total
            covered = any(
                delivered is not None and delivered < snapshot_from
                for delivered in (_fundos_net_delivery_date(row) for row in page)
            )
            if exhausted or covered:
                return rows, True
        return rows, False

    async def _document(
        self,
        client: httpx.AsyncClient,
        row: dict[str, Any],
    ) -> list[IncomeEventObservation]:
        key = (str(row["id"]), _positive_int(row.get("versao")))
        async with self._document_lock:
            cached = self._documents.get(key)
            if cached is not None and _fresh(
                cached[0], self.settings.income_source_index_ttl_seconds
            ):
                return cached[1]
            task = self._document_tasks.get(key)
            if task is None:
                task = asyncio.create_task(self._fetch_document(client, row, key))
                self._document_tasks[key] = task
        try:
            events = await task
            async with self._document_lock:
                self._documents[key] = (time.monotonic(), events)
                _trim_ttl_cache(self._documents, self.settings.fundos_net_scan_limit)
            return events
        finally:
            async with self._document_lock:
                if self._document_tasks.get(key) is task:
                    self._document_tasks.pop(key, None)

    async def _fetch_document(
        self,
        client: httpx.AsyncClient,
        row: dict[str, Any],
        key: tuple[str, int],
    ) -> list[IncomeEventObservation]:
        document = await client.get(
            "/fnet/publico/downloadDocumento",
            params={"id": key[0]},
        )
        document.raise_for_status()
        content = await read_bounded_body(document, self.settings.income_document_max_bytes)
        return parse_fundos_net_xml(
            content,
            document_id=key[0],
            version=key[1],
            source_status=_fnet_status(row),
        )


def _fundamentus_observation(
    instrument: IncomeInstrumentRequest,
    dividend: Dividend,
    ordinal: int,
) -> IncomeEventObservation:
    assert dividend.ex_date is not None
    assert dividend.payment_date is not None
    assert dividend.value is not None
    identity = "|".join(
        (
            instrument.ticker,
            dividend.type or "Provento",
            dividend.ex_date.isoformat(),
            dividend.payment_date.isoformat(),
            str(dividend.value),
            str(ordinal),
        )
    )
    return IncomeEventObservation(
        source="fundamentus",
        lineage="aggregator:fundamentus",
        source_event_id=f"fundamentus:{hashlib.sha256(identity.encode()).hexdigest()[:40]}",
        ticker=instrument.ticker,
        isin=instrument.isin,
        event_type=dividend.type or "Provento",
        ex_date=dividend.ex_date,
        payment_date=dividend.payment_date,
        unit_price=dividend.value,
        authority=20,
        payload_hash=hashlib.sha256(
            json.dumps(dividend.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest(),
    )


def _usable_dividend(dividend: Dividend) -> bool:
    return bool(
        dividend.ex_date
        and dividend.payment_date
        and dividend.value is not None
        and dividend.value > 0
    )


def _coverage(
    source: str,
    ticker: str,
    status: str,
    complete: bool,
    detail: str | None = None,
) -> IncomeSourceCoverage:
    return IncomeSourceCoverage(
        source=source,
        ticker=ticker,
        status=status,
        complete=complete,
        detail=detail,
    )


def _instrument_results(
    source: str,
    instruments: Sequence[IncomeInstrumentRequest],
    results: Sequence[list[IncomeEventObservation] | BaseException],
) -> IncomeSourceResult:
    observations: list[IncomeEventObservation] = []
    coverage: list[IncomeSourceCoverage] = []
    for instrument, result in zip(instruments, results, strict=True):
        if isinstance(result, BaseException):
            coverage.append(_coverage(source, instrument.ticker, "failed", False, str(result)))
            continue
        observations.extend(result)
        coverage.append(_coverage(source, instrument.ticker, "complete", True))
    return IncomeSourceResult(observations, coverage)


def _issuer_code(ticker: str) -> str:
    match = re.match(r"[A-Z]+", ticker.upper())
    return (match.group(0) if match else ticker.upper())[:4]


def _distinct_b3_isins(payload: Any) -> int:
    rows = payload if isinstance(payload, list) else []
    company = next((row for row in rows if isinstance(row, dict)), None)
    if company is None:
        return 0
    return len(
        {
            isin
            for row in company.get("cashDividends") or []
            if isinstance(row, dict)
            and (isin := str(row.get("isinCode") or row.get("assetIssued") or "").strip().upper())
        }
    )


def _limits(settings: Settings) -> httpx.Limits:
    return httpx.Limits(
        max_connections=settings.max_connections,
        max_keepalive_connections=settings.max_keepalive_connections,
    )


def _read_cvm_zip(payload: bytes) -> list[dict[str, str]]:
    with open_validated_zip(payload) as archive:
        members = [item for item in archive.infolist() if not item.is_dir()]
        if len(members) != 1:
            raise ValueError("CVM IPE archive must contain exactly one CSV")
        with archive.open(members[0]) as source:
            wrapper = io.TextIOWrapper(source, encoding="iso-8859-1", newline="")
            return list(csv.DictReader(wrapper, delimiter=";"))


def _latest_cvm_documents(
    rows: list[dict[str, str]],
    *,
    limit: int = 24,
) -> list[dict[str, str]]:
    latest: dict[str, dict[str, str]] = {}
    for row in rows:
        protocol = _cvm_document_id(row)
        existing = latest.get(protocol)
        if existing is None or _positive_int(row.get("Versao")) > _positive_int(
            existing.get("Versao")
        ):
            latest[protocol] = row
    return sorted(latest.values(), key=lambda row: row.get("Data_Entrega", ""), reverse=True)[
        : max(limit, 0)
    ]


def _cvm_income_adjustment(row: dict[str, str]) -> bool:
    if _fold(row.get("Categoria", "")) != "COMUNICADO AO MERCADO":
        return False
    subject = _fold(row.get("Assunto", ""))
    return "ATUALIZACAO MONETARIA" in subject and any(
        term in subject for term in ("REMUNERACAO", "PROVENT", "DIVID", "JUROS")
    )


def _cvm_document_id(row: dict[str, str]) -> str:
    link = row.get("Link_Download", "")
    match = re.search(r"numProtocolo=(\d+)", link)
    return (
        match.group(1)
        if match
        else (row.get("Protocolo_Entrega") or hashlib.sha256(link.encode()).hexdigest()[:24])
    )


def _pdf_text(content: bytes) -> str:
    reader = PdfReader(io.BytesIO(content), strict=True)
    if len(reader.pages) > 20:
        raise ValueError("income report has too many pages")
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _fundos_net_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("invalid Fundos.NET response")
    return [row for row in payload["data"] if isinstance(row, dict) and row.get("id")]


def _fundos_net_delivery_date(row: dict[str, Any]) -> date | None:
    raw = str(row.get("dataEntrega") or "").strip().split(" ", maxsplit=1)[0]
    try:
        return datetime.strptime(raw, "%d/%m/%Y").date()
    except ValueError:
        return None


def _fundos_net_candidates(
    rows: list[dict[str, Any]],
    requested: dict[str, IncomeInstrumentRequest],
    fallback: int,
) -> list[dict[str, Any]]:
    terms = {
        token
        for instrument in requested.values()
        for token in _distinctive_tokens(instrument.name or "")
    }
    matched = [
        row
        for row in rows
        if terms.intersection(
            _distinctive_tokens(
                " ".join(
                    str(row.get(field) or "")
                    for field in ("descricaoFundo", "nomePregao", "informacoesAdicionais")
                )
            )
        )
    ]
    selected = [*matched, *rows[: max(fallback, 0)]]
    return list({str(row["id"]): row for row in selected}.values())


def _distinctive_tokens(value: str) -> set[str]:
    ignored = {"FUNDO", "INVESTIMENTO", "IMOBILIARIO", "RESPONSABILIDADE", "LIMITADA", "FII"}
    return {
        token
        for token in re.findall(r"[A-Z0-9]+", _fold(value))
        if len(token) >= 4 and token not in ignored
    }


def _fnet_status(row: dict[str, Any]) -> str:
    status = str(row.get("status") or row.get("situacaoDocumento") or "").upper()
    return "cancelled" if status in {"IC", "C", "CANCELADO"} else "active"


def _positive_int(value: Any) -> int:
    try:
        return max(int(value or 1), 1)
    except (TypeError, ValueError):
        return 1


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(
        character for character in normalized if not unicodedata.combining(character)
    ).upper()


def _fresh(stored_at: float, ttl_seconds: int) -> bool:
    return time.monotonic() - stored_at < max(ttl_seconds, 0)


def _trim_ttl_cache[CacheKey, CacheValue](
    cache: dict[CacheKey, tuple[float, CacheValue]],
    maximum: int,
) -> None:
    overflow = len(cache) - max(maximum, 1)
    if overflow <= 0:
        return
    oldest = sorted(cache, key=lambda key: cache[key][0])[:overflow]
    for key in oldest:
        cache.pop(key, None)


__all__ = [
    "FundamentusIncomeSource",
    "FundosNetIncomeSource",
    "IncomeSource",
    "IncomeSourceResult",
    "OfficialCompanyIncomeSource",
    "StatusInvestIncomeSource",
]
