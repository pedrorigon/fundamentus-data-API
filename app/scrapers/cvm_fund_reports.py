from __future__ import annotations

import asyncio
import csv
import io
import unicodedata
import zipfile
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from time import monotonic

import httpx

from app.config import Settings
from app.core.archive_safety import (
    ArchiveSafetyError,
    open_validated_zip,
    read_bounded_body,
)
from app.core.errors import APIError, ProviderInvalidResponseError, ProviderUnavailableError
from app.models import InstrumentMetadata, InstrumentType

SOURCE_CVM = "cvm"
_FII_HISTORY_YEARS = 4
_FI_INFRA_HISTORY_MONTHS = 18


@dataclass(frozen=True)
class FundReportPoint:
    as_of: date
    nav_per_share: Decimal
    monthly_distribution_yield: Decimal | None = None
    monthly_nav_return: Decimal | None = None
    monthly_effective_return: Decimal | None = None
    net_assets: Decimal | None = None
    issued_shares: Decimal | None = None
    shareholder_count: Decimal | None = None
    administration_fee_ratio: Decimal | None = None
    total_assets: Decimal | None = None
    total_liabilities: Decimal | None = None
    property_assets: Decimal | None = None
    credit_assets: Decimal | None = None
    liquid_assets: Decimal | None = None
    inception_date: date | None = None
    segment: str | None = None
    administrator: str | None = None


@dataclass(frozen=True)
class FundArchiveFailure:
    path: str
    code: str


@dataclass(frozen=True)
class FundReportSeries:
    cnpj: str | None = None
    reports: tuple[FundReportPoint, ...] = ()
    archive_failures: tuple[FundArchiveFailure, ...] = ()


class CvmFundReportProvider:
    """Load official CVM fund reports with bounded, shared in-process caches."""

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self._report_cache_max_entries = max(settings.cvm_report_cache_max_entries, 1)
        self._archive_cache_max_entries = max(settings.cvm_archive_cache_max_entries, 1)
        self._archive_cache_max_bytes = max(settings.cvm_archive_cache_max_bytes, 1)
        self._cache_ttl_seconds = max(settings.instrument_data_ttl_seconds, 0)
        self._archives: OrderedDict[str, tuple[float, bytes | None]] = OrderedDict()
        self._archive_cache_bytes = 0
        self._archive_tasks: dict[str, asyncio.Task[bytes | None]] = {}
        self._archive_lock = asyncio.Lock()
        self._reports: OrderedDict[
            tuple[str, str, str, str, date], tuple[float, FundReportSeries]
        ] = OrderedDict()

    async def reports(
        self,
        instrument: InstrumentMetadata | None,
        *,
        cnpj: str | None = None,
        today: date | None = None,
    ) -> FundReportSeries:
        if instrument is None:
            return FundReportSeries()
        reference = today or datetime.now(UTC).date()
        cache_key = (
            instrument.instrument_type.value,
            instrument.ticker.strip().upper(),
            (instrument.isin or "").strip().upper(),
            _digits(cnpj),
            reference,
        )
        cached = self._reports.get(cache_key)
        if cached is not None:
            expires_at, result = cached
            if expires_at > monotonic():
                self._reports.move_to_end(cache_key)
                return result
            self._reports.pop(cache_key, None)
        if instrument.instrument_type is InstrumentType.fi_infra:
            result = await self._fi_infra_reports(cnpj, reference)
        elif instrument.instrument_type in {InstrumentType.fii, InstrumentType.fiagro}:
            result = await self._listed_fund_reports(instrument, reference)
        else:
            result = FundReportSeries()
        self._reports[cache_key] = (monotonic() + self._cache_ttl_seconds, result)
        self._reports.move_to_end(cache_key)
        _trim_lru(self._reports, self._report_cache_max_entries)
        return result

    async def _listed_fund_reports(
        self,
        instrument: InstrumentMetadata,
        reference: date,
    ) -> FundReportSeries:
        years = range(reference.year - _FII_HISTORY_YEARS + 1, reference.year + 1)
        paths = [f"/dados/FII/DOC/INF_MENSAL/DADOS/inf_mensal_fii_{year}.zip" for year in years]
        payloads, failures = await self._download_many(paths)
        if not payloads and failures:
            raise failures[0][1]
        archive_failures = [
            FundArchiveFailure(path=path, code=error.code) for path, error in failures
        ]
        candidates = [parse_fii_reports(payload, instrument) for payload in payloads]
        if instrument.instrument_type is InstrumentType.fiagro:
            fiagro_paths = [
                (f"/dados/FIAGRO/DOC/INF_MENSAL/DADOS/inf_mensal_fiagro_{year}{month:02d}.zip")
                for year, month in _months_until(reference, 18)
                if (year, month) >= (2025, 8)
            ]
            fiagro_payloads, fiagro_failures = await self._download_many(fiagro_paths)
            if (
                not fiagro_payloads
                and fiagro_failures
                and not any(candidate.reports for candidate in candidates)
            ):
                raise fiagro_failures[0][1]
            archive_failures.extend(
                FundArchiveFailure(path=path, code=error.code) for path, error in fiagro_failures
            )
            candidates.extend(
                parse_fiagro_reports(payload, instrument) for payload in fiagro_payloads
            )
        merged = merge_report_series(candidates)
        return FundReportSeries(
            cnpj=merged.cnpj,
            reports=merged.reports,
            archive_failures=(*merged.archive_failures, *archive_failures),
        )

    async def _fi_infra_reports(
        self,
        cnpj: str | None,
        reference: date,
    ) -> FundReportSeries:
        normalized_cnpj = _digits(cnpj)
        if not normalized_cnpj:
            return FundReportSeries()
        paths = [
            (f"/dados/FI/DOC/INF_DIARIO/DADOS/inf_diario_fi_{year}{month:02d}.zip")
            for year, month in _months_until(reference, _FI_INFRA_HISTORY_MONTHS)
        ]
        payloads, failures = await self._download_many(paths)
        if not payloads and failures:
            raise failures[0][1]
        reports = [
            point
            for payload in payloads
            for point in parse_daily_fund_reports(payload, normalized_cnpj)
        ]
        return FundReportSeries(
            cnpj=normalized_cnpj,
            reports=_latest_version_by_month(reports),
            archive_failures=tuple(
                FundArchiveFailure(path=path, code=error.code) for path, error in failures
            ),
        )

    async def _download(self, path: str) -> bytes | None:
        async with self._archive_lock:
            self._purge_expired_archives()
            cached = self._archives.get(path)
            if cached is not None:
                self._archives.move_to_end(path)
                return cached[1]
            task = self._archive_tasks.get(path)
            if task is None:
                task = asyncio.create_task(self._fetch_and_cache(path))
                task.add_done_callback(_consume_task_exception)
                self._archive_tasks[path] = task
        return await asyncio.shield(task)

    async def _fetch_and_cache(self, path: str) -> bytes | None:
        task = asyncio.current_task()
        try:
            payload = await self._request(path)
            async with self._archive_lock:
                if self._archive_tasks.get(path) is task:
                    self._cache_archive(path, payload)
                    self._archive_tasks.pop(path, None)
            return payload
        except BaseException:
            async with self._archive_lock:
                if self._archive_tasks.get(path) is task:
                    self._archive_tasks.pop(path, None)
            raise

    def _cache_archive(self, path: str, payload: bytes | None) -> None:
        """Store one completed archive while holding ``_archive_lock``."""

        self._purge_expired_archives()
        self._remove_archive(path)
        if payload is not None and len(payload) > self._archive_cache_max_bytes:
            return
        self._archives[path] = (monotonic() + self._cache_ttl_seconds, payload)
        self._archives.move_to_end(path)
        if payload is not None:
            self._archive_cache_bytes += len(payload)
        self._trim_archive_cache()

    def _purge_expired_archives(self) -> None:
        now = monotonic()
        for path, (expires_at, _payload) in tuple(self._archives.items()):
            if expires_at <= now:
                self._remove_archive(path)

    def _trim_archive_cache(self) -> None:
        while (
            len(self._archives) > self._archive_cache_max_entries
            or self._archive_cache_bytes > self._archive_cache_max_bytes
        ):
            path, _entry = self._archives.popitem(last=False)
            self._remove_archive_bytes(_entry[1])

    def _remove_archive(self, path: str) -> None:
        entry = self._archives.pop(path, None)
        if entry is not None:
            self._remove_archive_bytes(entry[1])

    def _remove_archive_bytes(self, payload: bytes | None) -> None:
        if payload is not None:
            self._archive_cache_bytes -= len(payload)

    async def _download_many(
        self,
        paths: list[str],
    ) -> tuple[list[bytes], list[tuple[str, APIError]]]:
        results = await asyncio.gather(
            *(self._download(path) for path in paths),
            return_exceptions=True,
        )
        payloads: list[bytes] = []
        failures: list[tuple[str, APIError]] = []
        for path, result in zip(paths, results, strict=True):
            if isinstance(result, bytes):
                payloads.append(result)
            elif isinstance(result, APIError):
                failures.append((path, result))
            elif isinstance(result, BaseException):
                raise result
        return payloads, failures

    async def _request(self, path: str) -> bytes | None:
        try:
            async with httpx.AsyncClient(
                base_url=self.settings.cvm_open_data_base_url,
                timeout=httpx.Timeout(self.settings.cvm_request_timeout_seconds),
                transport=self.transport,
                follow_redirects=True,
                headers={"User-Agent": self.settings.user_agent},
            ) as client:
                async with client.stream("GET", path) as response:
                    if response.status_code == 404:
                        return None
                    if not 200 <= response.status_code < 300:
                        raise ProviderUnavailableError()
                    payload = await read_bounded_body(
                        response, self.settings.archive_download_max_bytes
                    )
                    with open_validated_zip(payload):
                        return payload
        except ProviderUnavailableError:
            raise
        except (ArchiveSafetyError, zipfile.BadZipFile) as exc:
            raise ProviderInvalidResponseError() from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError() from exc


def parse_fii_reports(
    payload: bytes,
    instrument: InstrumentMetadata,
) -> FundReportSeries:
    with open_validated_zip(payload) as archive:
        general_name = _member(archive, "geral")
        complement_name = _member(archive, "complemento")
        if general_name is None or complement_name is None:
            return FundReportSeries()
        asset_name = _member(archive, "ativo_passivo")
        matches = []
        for row in _rows(archive, general_name):
            isin = _text(row.get("Codigo_ISIN"))
            if not _same_isin(isin, instrument.isin):
                continue
            candidate_cnpj = _digits(row.get("CNPJ_Fundo_Classe") or row.get("CNPJ_Fundo"))
            name = _text(row.get("Nome_Fundo_Classe") or row.get("Nome_Fundo"))
            if candidate_cnpj:
                matches.append((_name_similarity(name, instrument.name), candidate_cnpj))
        cnpj = max(matches, key=lambda item: item[0])[1] if matches else None
        if cnpj is None:
            return FundReportSeries()
        metadata = {
            _row_date_key(row): row
            for row in _rows(archive, general_name)
            if _digits(row.get("CNPJ_Fundo_Classe") or row.get("CNPJ_Fundo")) == cnpj
            and _row_date_key(row) is not None
        }
        assets = (
            {
                _row_date_key(row): row
                for row in _rows(archive, asset_name)
                if _digits(row.get("CNPJ_Fundo_Classe") or row.get("CNPJ_Fundo")) == cnpj
                and _row_date_key(row) is not None
            }
            if asset_name is not None
            else {}
        )
        reports = [
            point
            for row in _rows(archive, complement_name)
            if _digits(row.get("CNPJ_Fundo_Classe") or row.get("CNPJ_Fundo")) == cnpj
            and (
                point := _monthly_report(
                    row,
                    percentage_points=False,
                    metadata=metadata.get(_row_date_key(row)),
                    assets=assets.get(_row_date_key(row)),
                )
            )
            is not None
        ]
    return FundReportSeries(cnpj=cnpj, reports=_latest_version_by_date(reports))


def parse_fiagro_reports(
    payload: bytes,
    instrument: InstrumentMetadata,
) -> FundReportSeries:
    with open_validated_zip(payload) as archive:
        member = _member(archive, "inf_mensal_fiagro_", exclude="subclasse")
        if member is None:
            return FundReportSeries()
        matches: list[tuple[Decimal, str, FundReportPoint]] = []
        for row in _rows(archive, member):
            if not _same_isin(_text(row.get("Codigo_ISIN")), instrument.isin):
                continue
            cnpj = _digits(row.get("CNPJ_Classe"))
            point = _monthly_report(row, percentage_points=True)
            if cnpj and point is not None:
                matches.append(
                    (
                        _name_similarity(_text(row.get("Nome_Classe")), instrument.name),
                        cnpj,
                        point,
                    )
                )
    if not matches:
        return FundReportSeries()
    _score, cnpj, _point = max(matches, key=lambda item: item[0])
    reports = tuple(item[2] for item in matches if item[1] == cnpj)
    return FundReportSeries(cnpj=cnpj, reports=_latest_version_by_date(reports))


def parse_daily_fund_reports(payload: bytes, cnpj: str) -> tuple[FundReportPoint, ...]:
    with open_validated_zip(payload) as archive:
        member = _member(archive, "inf_diario_fi_")
        if member is None:
            return ()
        points = []
        for row in _rows(archive, member):
            if _digits(row.get("CNPJ_FUNDO_CLASSE")) != cnpj:
                continue
            as_of = _date(row.get("DT_COMPTC"))
            nav = _decimal(row.get("VL_QUOTA"))
            if as_of is not None and nav is not None and nav > 0:
                points.append(FundReportPoint(as_of=as_of, nav_per_share=nav))
    return _latest_version_by_month(points)


def merge_report_series(series: list[FundReportSeries]) -> FundReportSeries:
    archive_failures = tuple(failure for item in series for failure in item.archive_failures)
    populated = [item for item in series if item.reports]
    if not populated:
        return FundReportSeries(archive_failures=archive_failures)
    cnpj_counts: dict[str, int] = {}
    for item in populated:
        if item.cnpj:
            cnpj_counts[item.cnpj] = cnpj_counts.get(item.cnpj, 0) + len(item.reports)
    cnpj = max(cnpj_counts, key=lambda key: cnpj_counts[key]) if cnpj_counts else None
    reports = [
        report for item in populated if cnpj is None or item.cnpj == cnpj for report in item.reports
    ]
    return FundReportSeries(
        cnpj=cnpj,
        reports=_latest_version_by_date(reports),
        archive_failures=archive_failures,
    )


def _monthly_report(
    row: dict[str, str],
    *,
    percentage_points: bool,
    metadata: dict[str, str] | None = None,
    assets: dict[str, str] | None = None,
) -> FundReportPoint | None:
    as_of = _date(row.get("Data_Referencia"))
    nav = _decimal(row.get("Valor_Patrimonial_Cotas"))
    if as_of is None or nav is None or nav <= 0:
        return None
    return FundReportPoint(
        as_of=as_of,
        nav_per_share=nav,
        monthly_distribution_yield=_ratio(
            row.get("Percentual_Dividend_Yield_Mes") or row.get("Dividend_Yield_Mes"),
            percentage_points=percentage_points,
        ),
        monthly_nav_return=_ratio(
            row.get("Percentual_Rentabilidade_Patrimonial_Mes")
            or row.get("Rentabilidade_Patrimonial_Mes"),
            percentage_points=percentage_points,
        ),
        monthly_effective_return=_ratio(
            row.get("Percentual_Rentabilidade_Efetiva_Mes") or row.get("Rentabilidade_Efetiva_Mes"),
            percentage_points=percentage_points,
        ),
        net_assets=_decimal(row.get("Patrimonio_Liquido")),
        issued_shares=_decimal(
            row.get("Cotas_Emitidas")
            or row.get("Quantidade_Cotas_Emitidas")
            or (metadata or {}).get("Quantidade_Cotas_Emitidas")
        ),
        shareholder_count=_decimal(row.get("Total_Numero_Cotistas") or row.get("Numero_Cotistas")),
        administration_fee_ratio=_annualized_fee(
            row.get("Percentual_Despesas_Taxa_Administracao"),
            percentage_points=percentage_points,
        ),
        total_assets=_first_or_sum(
            assets,
            "Valor_Ativo",
            (
                "Total_Necessidades_Liquidez",
                "Total_Investido",
                "Valores_Receber",
            ),
        ),
        total_liabilities=_decimal((assets or {}).get("Total_Passivo")),
        property_assets=_first_or_sum(
            assets,
            "Direitos_Bens_Imoveis",
            (
                "Terrenos",
                "Imoveis_Renda_Acabados",
                "Imoveis_Renda_Construcao",
                "Imoveis_Venda_Acabados",
                "Imoveis_Venda_Construcao",
                "Outros_Direitos_Reais",
            ),
        ),
        credit_assets=_sum_fields(
            assets,
            (
                "CRI",
                "CRI_CRA",
                "Letras_Hipotecarias",
                "LCI",
                "LCI_LCA",
                "LIG",
                "Debentures",
            ),
        ),
        liquid_assets=_first_or_sum(
            assets,
            "Total_Necessidades_Liquidez",
            (
                "Disponibilidades",
                "Titulos_Publicos",
                "Fundos_Renda_Fixa",
            ),
        ),
        inception_date=_date((metadata or {}).get("Data_Funcionamento")),
        segment=_text((metadata or {}).get("Segmento_Atuacao")),
        administrator=_text((metadata or {}).get("Nome_Administrador")),
    )


def _row_date_key(row: dict[str, str]) -> date | None:
    return _date(row.get("Data_Referencia"))


def _annualized_fee(value: str | None, *, percentage_points: bool) -> Decimal | None:
    monthly = _ratio(value, percentage_points=percentage_points)
    return monthly * Decimal("12") if monthly is not None and monthly >= 0 else None


def _sum_fields(
    row: dict[str, str] | None,
    fields: tuple[str, ...],
) -> Decimal | None:
    if row is None:
        return None
    values = [value for field in fields if (value := _decimal(row.get(field))) is not None]
    return sum(values, Decimal("0")) if values else None


def _first_or_sum(
    row: dict[str, str] | None,
    total_field: str,
    component_fields: tuple[str, ...],
) -> Decimal | None:
    total = _decimal((row or {}).get(total_field))
    return total if total is not None else _sum_fields(row, component_fields)


def _rows(archive: zipfile.ZipFile, member: str) -> Iterator[dict[str, str]]:
    with archive.open(member) as raw:
        with io.TextIOWrapper(raw, encoding="latin-1", newline="") as text:
            yield from csv.DictReader(text, delimiter=";")


def _member(
    archive: zipfile.ZipFile,
    included: str,
    *,
    exclude: str | None = None,
) -> str | None:
    return next(
        (
            name
            for name in archive.namelist()
            if included in name.lower() and (exclude is None or exclude not in name.lower())
        ),
        None,
    )


def _same_isin(first: str | None, second: str | None) -> bool:
    if not first or not second:
        return False
    return first[:10].upper() == second[:10].upper()


def _name_similarity(first: str | None, second: str | None) -> Decimal:
    if not first or not second:
        return Decimal("0")
    ratio = SequenceMatcher(None, _fold(first), _fold(second)).ratio()
    return Decimal(str(ratio))


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return " ".join(normalized.encode("ascii", "ignore").decode("ascii").upper().split())


def _latest_version_by_date(
    reports: list[FundReportPoint] | tuple[FundReportPoint, ...],
) -> tuple[FundReportPoint, ...]:
    by_date = {report.as_of: report for report in reports}
    return tuple(by_date[key] for key in sorted(by_date))


def _latest_version_by_month(
    reports: list[FundReportPoint] | tuple[FundReportPoint, ...],
) -> tuple[FundReportPoint, ...]:
    by_month: dict[tuple[int, int], FundReportPoint] = {}
    for report in reports:
        key = (report.as_of.year, report.as_of.month)
        current = by_month.get(key)
        if current is None or report.as_of > current.as_of:
            by_month[key] = report
    return tuple(by_month[key] for key in sorted(by_month))


def _months_until(reference: date, count: int) -> tuple[tuple[int, int], ...]:
    year, month = reference.year, reference.month
    result = []
    for _index in range(count):
        result.append((year, month))
        month -= 1
        if month == 0:
            year -= 1
            month = 12
    return tuple(result)


def _ratio(value: str | None, *, percentage_points: bool) -> Decimal | None:
    parsed = _decimal(value)
    if parsed is None:
        return None
    return parsed / Decimal("100") if percentage_points else parsed


def _decimal(value: str | None) -> Decimal | None:
    text = _text(value)
    if text is None:
        return None
    try:
        return Decimal(text.replace(",", "."))
    except InvalidOperation:
        return None


def _date(value: str | None) -> date | None:
    text = _text(value)
    if text is None:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _digits(value: str | None) -> str:
    return "".join(character for character in value or "" if character.isdigit())


def _trim_lru[CacheKey, CacheValue](
    cache: OrderedDict[CacheKey, CacheValue],
    maximum: int,
) -> None:
    while len(cache) > max(maximum, 1):
        cache.popitem(last=False)


def _consume_task_exception(task: asyncio.Task[bytes | None]) -> None:
    if not task.cancelled():
        task.exception()


def _text(value: str | None) -> str | None:
    text = value.strip() if value is not None else ""
    return text or None
