"""Parse CVM open-data financial statement archives (DFP/ITR).

CVM publishes standardized statements as yearly ZIP archives of latin-1 CSV files.
Account codes (``CD_CONTA``) follow a fixed chart of accounts, which is what makes
the extraction stable across companies and years.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from io import TextIOWrapper
from zipfile import BadZipFile, ZipFile

from app.core.archive_safety import ArchiveSafetyError, open_validated_zip

SOURCE_CVM = "cvm"

_ENCODING = "latin-1"
_DELIMITER = ";"
_LAST_PERIOD = "ÚLTIMO"

# ``ESCALA_MOEDA`` reports whether values are expressed in units or thousands.
_SCALES = {"UNIDADE": Decimal("1"), "MIL": Decimal("1000")}

# Standardized CVM chart-of-accounts codes.
ACCOUNT_REVENUE = "3.01"
ACCOUNT_GROSS_PROFIT = "3.03"
ACCOUNT_EBIT = "3.05"
ACCOUNT_FINANCIAL_RESULT = "3.06"
ACCOUNT_NET_INCOME = "3.11"
ACCOUNT_EQUITY = "2.03"
ACCOUNT_TOTAL_ASSETS = "1"
ACCOUNT_CURRENT_ASSETS = "1.01"
ACCOUNT_CURRENT_LIABILITIES = "2.01"
ACCOUNT_CASH = "1.01.01"
ACCOUNT_SHORT_TERM_INVESTMENTS = "1.01.02"
ACCOUNT_CURRENT_DEBT = "2.01.04"
ACCOUNT_LONG_TERM_DEBT = "2.02.01"
ACCOUNT_OPERATING_CASH_FLOW = "6.01"
ACCOUNT_INVESTING_CASH_FLOW = "6.02"

# Depreciation lines live under the cash-flow statement with company-specific
# sub-codes, so they are matched by label instead of code.
_DEPRECIATION_TOKENS = ("DEPRECIA", "AMORTIZA")
_MEANINGFUL_ACCOUNT_CODES = (
    ACCOUNT_REVENUE,
    ACCOUNT_NET_INCOME,
    ACCOUNT_EQUITY,
    ACCOUNT_TOTAL_ASSETS,
)


@dataclass(frozen=True)
class StatementPeriod:
    """A single reported period for one company."""

    cnpj: str
    cvm_code: str
    company_name: str
    reference_date: date
    period_start: date | None
    period_end: date
    consolidated: bool
    accounts: dict[str, Decimal] = field(default_factory=dict)
    account_labels: dict[str, str] = field(default_factory=dict)
    depreciation: Decimal | None = None
    published_at: date | None = None

    def account(self, code: str) -> Decimal | None:
        return self.accounts.get(code)

    def account_by_label(self, *labels: str, prefix: str | None = None) -> Decimal | None:
        """Return the account matching the earliest label given.

        Labels are tried in the order the caller listed them, because several
        may be present in the same filing and only the first expresses the
        wanted figure.

        ``prefix`` restricts the search to one statement group. The same label
        appears in different statements — the value added statement repeats the
        wording of the income statement, for instance — so without it a lookup
        can answer with a figure from an unrelated statement.
        """
        by_label: dict[str, Decimal] = {}
        for code, label in self.account_labels.items():
            if code not in self.accounts:
                continue
            if prefix is not None and not code.startswith(prefix):
                continue
            by_label.setdefault(_fold(label), self.accounts[code])
        return next(
            (value for label in labels if (value := by_label.get(_fold(label))) is not None),
            None,
        )


@dataclass(frozen=True)
class ShareCapital:
    """Issued and treasury share counts from ``composicao_capital``."""

    cnpj: str
    reference_date: date
    common_shares: Decimal
    preferred_shares: Decimal
    treasury_shares: Decimal

    @property
    def total_shares(self) -> Decimal:
        return self.common_shares + self.preferred_shares

    @property
    def outstanding_shares(self) -> Decimal:
        outstanding = self.total_shares - self.treasury_shares
        return outstanding if outstanding > 0 else self.total_shares


@dataclass(frozen=True)
class CompanyRegistration:
    """Company registry entry used to map CNPJ to sector."""

    cnpj: str
    cvm_code: str
    corporate_name: str
    trade_name: str | None
    sector: str | None
    status: str


def parse_statement_archive(
    payload: bytes,
    *,
    cnpjs: set[str] | None = None,
    prefer_consolidated: bool = True,
) -> dict[str, list[StatementPeriod]]:
    """Extract statement periods per CNPJ from a DFP/ITR archive.

    Consolidated statements (``_con``) describe the economic group and are
    preferred; individual statements (``_ind``) are used only for companies that
    do not publish consolidated figures.
    """
    wanted = _normalized_cnpjs(cnpjs)
    try:
        with open_validated_zip(payload) as archive:
            names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
            periods = _collect_periods(archive, names, wanted)
    except (ArchiveSafetyError, BadZipFile, OSError, UnicodeError):
        return {}
    return _select_periods(periods, prefer_consolidated=prefer_consolidated)


def parse_share_capital(
    payload: bytes, *, cnpjs: set[str] | None = None
) -> dict[str, ShareCapital]:
    """Extract the most recent share counts per CNPJ from a statement archive."""
    wanted = _normalized_cnpjs(cnpjs)
    results: dict[str, ShareCapital] = {}
    try:
        with open_validated_zip(payload) as archive:
            names = [
                name
                for name in archive.namelist()
                if "composicao_capital" in name.lower() and name.lower().endswith(".csv")
            ]
            for name in names:
                for row in _iter_rows(archive, name):
                    _add_share_capital(results, row, wanted)
    except (ArchiveSafetyError, BadZipFile, OSError, UnicodeError):
        return {}
    return results


def parse_company_registry(payload: bytes) -> dict[str, CompanyRegistration]:
    """Parse the CVM company registry CSV keyed by CNPJ."""
    registry: dict[str, CompanyRegistration] = {}
    try:
        rows = _iter_csv_lines(payload.decode(_ENCODING).splitlines())
    except UnicodeError:
        return {}
    for row in rows:
        cnpj = _digits(row.get("CNPJ_CIA"))
        cvm_code = (row.get("CD_CVM") or "").strip()
        corporate_name = (row.get("DENOM_SOCIAL") or "").strip()
        if not cnpj or not corporate_name:
            continue
        registry[cnpj] = CompanyRegistration(
            cnpj=cnpj,
            cvm_code=cvm_code,
            corporate_name=corporate_name,
            trade_name=(row.get("DENOM_COMERC") or "").strip() or None,
            sector=(row.get("SETOR_ATIV") or "").strip() or None,
            status=(row.get("SIT") or "").strip(),
        )
    return registry


def _collect_periods(
    archive: ZipFile,
    names: list[str],
    wanted: set[str] | None,
) -> dict[tuple[str, date, bool, str], _PeriodAccumulator]:
    publications = _collect_publications(archive, names, wanted)
    accumulators: dict[tuple[str, date, bool, str], _PeriodAccumulator] = {}
    for name in names:
        lowered = name.lower()
        if "composicao_capital" in lowered or "parecer" in lowered:
            continue
        consolidated = "_con_" in lowered
        if not consolidated and "_ind_" not in lowered:
            continue
        for row in _iter_rows(archive, name):
            _add_account(
                accumulators,
                row,
                wanted,
                consolidated=consolidated,
                publications=publications,
            )
    return accumulators


def _collect_publications(
    archive: ZipFile,
    names: list[str],
    wanted: set[str] | None,
) -> dict[tuple[str, date, str], date]:
    """Read filing receipt dates from the archive's document metadata CSV."""
    publications: dict[tuple[str, date, str], date] = {}
    for name in names:
        lowered = name.lower()
        if "_con_" in lowered or "_ind_" in lowered or "composicao_capital" in lowered:
            continue
        for row in _iter_rows(archive, name):
            cnpj = _digits(row.get("CNPJ_CIA"))
            reference = _iso_date(row.get("DT_REFER"))
            received = _iso_date(row.get("DT_RECEB"))
            if (
                not cnpj
                or reference is None
                or received is None
                or (wanted is not None and cnpj not in wanted)
            ):
                continue
            version = (row.get("VERSAO") or "").strip()
            key = (cnpj, reference, version)
            previous = publications.get(key)
            if previous is None or received > previous:
                publications[key] = received
    return publications


def _add_account(
    accumulators: dict[tuple[str, date, bool, str], _PeriodAccumulator],
    row: dict[str, str],
    wanted: set[str] | None,
    *,
    consolidated: bool,
    publications: dict[tuple[str, date, str], date],
) -> None:
    if (row.get("ORDEM_EXERC") or "").strip().upper() != _LAST_PERIOD:
        return
    cnpj = _digits(row.get("CNPJ_CIA"))
    if not cnpj or (wanted is not None and cnpj not in wanted):
        return
    period_end = _iso_date(row.get("DT_FIM_EXERC")) or _iso_date(row.get("DT_REFER"))
    reference = _iso_date(row.get("DT_REFER"))
    if period_end is None or reference is None:
        return
    value = _decimal(row.get("VL_CONTA"))
    if value is None:
        return
    code = (row.get("CD_CONTA") or "").strip()
    if not code:
        return
    scale = _account_scale(code, value, row.get("ESCALA_MOEDA"))

    version = (row.get("VERSAO") or "").strip()
    key = (cnpj, period_end, consolidated, version)
    accumulator = accumulators.get(key)
    if accumulator is None:
        accumulator = _PeriodAccumulator(
            cnpj=cnpj,
            cvm_code=(row.get("CD_CVM") or "").strip(),
            company_name=(row.get("DENOM_CIA") or "").strip(),
            reference_date=reference,
            period_start=_iso_date(row.get("DT_INI_EXERC")),
            period_end=period_end,
            consolidated=consolidated,
            version=version,
            published_at=_iso_date(row.get("DT_RECEB"))
            or publications.get((cnpj, reference, version)),
        )
        accumulators[key] = accumulator
    period_start = _iso_date(row.get("DT_INI_EXERC"))
    if accumulator.period_start is None and period_start is not None:
        accumulator.period_start = period_start
    if accumulator.published_at is None:
        accumulator.published_at = publications.get((cnpj, reference, version))
    accumulator.add(code, value * scale, row.get("DS_CONTA"))


def _select_periods(
    accumulators: dict[tuple[str, date, bool, str], _PeriodAccumulator],
    *,
    prefer_consolidated: bool,
) -> dict[str, list[StatementPeriod]]:
    by_company: dict[str, dict[date, list[_PeriodAccumulator]]] = {}
    for accumulator in accumulators.values():
        by_company.setdefault(accumulator.cnpj, {}).setdefault(accumulator.period_end, []).append(
            accumulator
        )

    results: dict[str, list[StatementPeriod]] = {}
    for cnpj, periods in by_company.items():
        selected = [
            _prefer(candidates, prefer_consolidated=prefer_consolidated).build()
            for _, candidates in sorted(periods.items())
        ]
        results[cnpj] = selected
    return results


def _prefer(
    candidates: list[_PeriodAccumulator],
    *,
    prefer_consolidated: bool,
) -> _PeriodAccumulator:
    meaningful = [candidate for candidate in candidates if _has_meaningful_financials(candidate)]
    eligible = meaningful or candidates
    if prefer_consolidated:
        consolidated = [candidate for candidate in eligible if candidate.consolidated]
        if consolidated:
            eligible = consolidated
    return max(
        eligible,
        key=lambda candidate: (
            _version_number(candidate.version),
            candidate.published_at or date.min,
            len(candidate.accounts),
        ),
    )


def _has_meaningful_financials(candidate: _PeriodAccumulator) -> bool:
    return any(
        candidate.accounts.get(code) not in {None, Decimal("0")}
        for code in _MEANINGFUL_ACCOUNT_CODES
    )


def _add_share_capital(
    results: dict[str, ShareCapital],
    row: dict[str, str],
    wanted: set[str] | None,
) -> None:
    cnpj = _digits(row.get("CNPJ_CIA"))
    if not cnpj or (wanted is not None and cnpj not in wanted):
        return
    reference = _iso_date(row.get("DT_REFER"))
    if reference is None:
        return
    existing = results.get(cnpj)
    if existing is not None and existing.reference_date >= reference:
        return
    common = _decimal(row.get("QT_ACAO_ORDIN_CAP_INTEGR")) or Decimal("0")
    preferred = _decimal(row.get("QT_ACAO_PREF_CAP_INTEGR")) or Decimal("0")
    total = _decimal(row.get("QT_ACAO_TOTAL_CAP_INTEGR")) or Decimal("0")
    if common + preferred <= 0 and total > 0:
        common = total
    results[cnpj] = ShareCapital(
        cnpj=cnpj,
        reference_date=reference,
        common_shares=common,
        preferred_shares=preferred,
        treasury_shares=_decimal(row.get("QT_ACAO_TOTAL_TESOURO")) or Decimal("0"),
    )


@dataclass
class _PeriodAccumulator:
    cnpj: str
    cvm_code: str
    company_name: str
    reference_date: date
    period_start: date | None
    period_end: date
    consolidated: bool
    version: str = ""
    accounts: dict[str, Decimal] = field(default_factory=dict)
    account_labels: dict[str, str] = field(default_factory=dict)
    depreciation: Decimal | None = None
    published_at: date | None = None

    def add(self, code: str, value: Decimal, label: str | None) -> None:
        self.accounts.setdefault(code, value)
        if label:
            self.account_labels.setdefault(code, label)
        if self.depreciation is None and _is_depreciation(code, label):
            self.depreciation = abs(value)

    def build(self) -> StatementPeriod:
        return StatementPeriod(
            cnpj=self.cnpj,
            cvm_code=self.cvm_code,
            company_name=self.company_name,
            reference_date=self.reference_date,
            period_start=self.period_start,
            period_end=self.period_end,
            consolidated=self.consolidated,
            accounts=dict(self.accounts),
            account_labels=dict(self.account_labels),
            depreciation=self.depreciation,
            published_at=self.published_at,
        )


def _is_depreciation(code: str, label: str | None) -> bool:
    if not code.startswith("6.") or not label:
        return False
    folded = _fold(label)
    return any(token in folded for token in _DEPRECIATION_TOKENS)


def _account_scale(
    code: str,
    value: Decimal,
    reported_scale: str | None,
) -> Decimal:
    scale_name = (reported_scale or "").strip().upper()
    if code.startswith(("3.99.01.", "3.99.02.")) and scale_name == "MIL":
        # Some CVM filings encode R$ 2.78 as 2780 while older ones encode it
        # directly as 2.78, despite both declaring the archive scale as MIL.
        return Decimal("0.001") if abs(value) >= Decimal("100") else Decimal("1")
    return _SCALES.get(scale_name, Decimal("1"))


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return normalized.encode("ascii", "ignore").decode("ascii").upper()


def _iter_rows(archive: ZipFile, name: str) -> Iterator[dict[str, str]]:
    with archive.open(name) as raw:
        stream = TextIOWrapper(raw, encoding=_ENCODING, newline="")
        header_line = stream.readline()
        if not header_line:
            return
        header = header_line.rstrip("\r\n").split(_DELIMITER)
        for line in stream:
            values = line.rstrip("\r\n").split(_DELIMITER)
            if len(values) != len(header):
                continue
            yield dict(zip(header, values, strict=True))


def _iter_csv_lines(lines: list[str]) -> Iterator[dict[str, str]]:
    if not lines:
        return
    header = lines[0].rstrip("\r\n").split(_DELIMITER)
    for line in lines[1:]:
        values = line.rstrip("\r\n").split(_DELIMITER)
        if len(values) != len(header):
            continue
        yield dict(zip(header, values, strict=True))


def _normalized_cnpjs(cnpjs: set[str] | None) -> set[str] | None:
    if cnpjs is None:
        return None
    normalized = {_digits(cnpj) for cnpj in cnpjs}
    return {cnpj for cnpj in normalized if cnpj}


def _digits(value: str | None) -> str:
    if not value:
        return ""
    return "".join(character for character in value if character.isdigit())


def _iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _version_number(value: str) -> Decimal:
    return _decimal(value) or Decimal("0")
