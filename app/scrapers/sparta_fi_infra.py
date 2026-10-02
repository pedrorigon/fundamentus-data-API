"""Verified monthly income observations from the JURO11 manager report."""

from __future__ import annotations

import asyncio
import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from hashlib import sha256
from io import BytesIO
from time import monotonic

import httpx
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.core.errors import ProviderInvalidResponseError, ProviderUnavailableError
from app.models import FundMonthlyDistribution

SOURCE = "sparta_manager"
_CNPJ = "42730834000100"
_MAX_PDF_BYTES = 5_000_000
_MONTHS = {
    "Jan": 1,
    "Fev": 2,
    "Mar": 3,
    "Abr": 4,
    "Mai": 5,
    "Jun": 6,
    "Jul": 7,
    "Ago": 8,
    "Set": 9,
    "Out": 10,
    "Nov": 11,
    "Dez": 12,
}
_MONTH_NAMES = (
    "Janeiro",
    "Fevereiro",
    "Março",
    "Abril",
    "Maio",
    "Junho",
    "Julho",
    "Agosto",
    "Setembro",
    "Outubro",
    "Novembro",
    "Dezembro",
)
_ROW = re.compile(
    r"\b(Jan|Fev|Mar|Abr|Mai|Jun|Jul|Ago|Set|Out|Nov|Dez)-(\d{2})\s+"
    r"(\d{2}/\d{2}/\d{4})\s+R\$\s*([\d.,]+)\b"
)
_CREDIT_ROW = re.compile(
    r"(?:^|\n)(\d{1,4}) Debêntures ([A-Z0-9]+)\s+(.*?)\s+"
    r"(AAA|AA\+|AA-|AA|A\+|A-|A|BBB\+|BBB-|BBB|BB\+|BB-|BB|B\+|B-|B|CCC|CC|C|D|S/R)\s+"
    r"(-?\d+,\d+)%\s+(\d+,\d+)\s+(\d+,\d+)%",
    re.S,
)
_CREDIT_ROW_START = re.compile(r"(?m)^(\d{1,4}) Debêntures ")
_CASH_ROW = re.compile(r"(?m)^(\d{1,4}) Caixa\s+AAA\s+-?\d+,\d+%\s+\d+,\d+\s+(\d+,\d+)%")
_PORTFOLIO_TOTAL = re.compile(r"(?m)^Total\s+-?\d+,\d+%\s+\d+,\d+\s+(\d+,\d+)%")


@dataclass(frozen=True)
class CreditHolding:
    row_number: int
    security_code: str
    issuer_and_sector: str
    disclosed_rating: str
    credit_spread: Decimal
    duration_years: Decimal
    portfolio_weight: Decimal


@dataclass(frozen=True)
class CreditPortfolio:
    report_as_of: date
    published_at: datetime | None
    holdings: tuple[CreditHolding, ...]
    cash_weight: Decimal
    document_digest: str | None = None
    document_url: str | None = None

    @property
    def reported_weight(self) -> Decimal:
        return sum((holding.portfolio_weight for holding in self.holdings), Decimal("0"))

    @property
    def unrated_weight(self) -> Decimal:
        return sum(
            (
                holding.portfolio_weight
                for holding in self.holdings
                if holding.disclosed_rating == "S/R"
            ),
            Decimal("0"),
        )


def _month_end(year: int, month: int) -> date:
    return date(year, month, monthrange(year, month)[1])


def _report_url(year: int, month: int) -> str:
    return f"https://sparta.com.br/uploads/JURO11_RelatorioMensal_{year}_{month:02d}.pdf"


def _matches_manager_cover(cover: str, *, report_year: int, report_month: int) -> bool:
    expected_period = f"{_MONTH_NAMES[report_month - 1]}/{report_year}"
    return (
        "JURO11" in cover
        and expected_period in cover
        and any(
            re.sub(r"\D", "", match) == _CNPJ for match in re.findall(r"CNPJ\s+([\d./-]+)", cover)
        )
    )


def parse_monthly_distributions(
    content: bytes,
    *,
    report_year: int,
    report_month: int,
    published_at: datetime | None = None,
) -> tuple[FundMonthlyDistribution, ...]:
    """Reject an incomplete or mismatched report instead of filling missing months."""
    if not content.startswith(b"%PDF-") or len(content) > _MAX_PDF_BYTES:
        raise ValueError("Invalid manager report document")
    try:
        reader = PdfReader(BytesIO(content), strict=False)
        if len(reader.pages) < 4 or len(reader.pages) > 50:
            raise ValueError("Unexpected manager report length")
        cover = reader.pages[0].extract_text() or ""
        distribution_page = reader.pages[3].extract_text() or ""
    except (PdfReadError, OSError, KeyError, IndexError) as error:
        raise ValueError("Manager report cannot be parsed") from error
    if (
        not _matches_manager_cover(cover, report_year=report_year, report_month=report_month)
        or "DISTRIBUIÇÕES DE RENDIMENTOS" not in distribution_page
    ):
        raise ValueError("Manager report identity or period does not match")

    report_as_of = _month_end(report_year, report_month)
    rows: dict[date, FundMonthlyDistribution] = {}
    for month_token, year_token, payment_token, value_token in _ROW.findall(distribution_page):
        year = 2000 + int(year_token)
        month = _MONTHS[month_token]
        reference_month = date(year, month, 1)
        try:
            payment_date = datetime.strptime(payment_token, "%d/%m/%Y").date()
            value = Decimal(value_token.replace(".", "").replace(",", "."))
        except (ValueError, InvalidOperation) as error:
            raise ValueError("Invalid manager distribution row") from error
        if (
            reference_month in rows
            or reference_month > report_as_of
            or payment_date <= _month_end(year, month)
            or (payment_date - _month_end(year, month)).days > 60
            or value < 0
            or value > 100
        ):
            raise ValueError("Inconsistent manager distribution row")
        rows[reference_month] = FundMonthlyDistribution(
            reference_month=reference_month,
            payment_date=payment_date,
            value=value,
            report_as_of=report_as_of,
            source=SOURCE,
            published_at=published_at,
        )
    if len(rows) != 12:
        raise ValueError("Manager report lacks twelve distribution months")
    ordered = tuple(rows[key] for key in sorted(rows))
    expected = date(report_year, report_month, 1)
    for row in reversed(ordered):
        if row.reference_month != expected:
            raise ValueError("Manager distribution months are not contiguous")
        expected = date(expected.year - (expected.month == 1), (expected.month - 2) % 12 + 1, 1)
    return ordered


def parse_credit_portfolio(
    content: bytes,
    *,
    report_year: int,
    report_month: int,
    published_at: datetime | None = None,
) -> CreditPortfolio:
    """Read the manager's issue-level inventory without inferring credit risk."""

    if not content.startswith(b"%PDF-") or len(content) > _MAX_PDF_BYTES:
        raise ValueError("Invalid manager report document")
    try:
        reader = PdfReader(BytesIO(content), strict=False)
        if len(reader.pages) < 8 or len(reader.pages) > 50:
            raise ValueError("Manager report lacks its portfolio section")
        cover = reader.pages[0].extract_text() or ""
        portfolio_pages = tuple(
            text
            for page in reader.pages
            if "# TIPO CÓDIGO" in (text := page.extract_text() or "")
            and "COMPOSIÇÃO DA CARTEIRA" in text
        )
    except (PdfReadError, OSError, KeyError, IndexError) as error:
        raise ValueError("Manager portfolio cannot be parsed") from error
    if (
        not _matches_manager_cover(cover, report_year=report_year, report_month=report_month)
        or not portfolio_pages
    ):
        raise ValueError("Manager portfolio identity or period does not match")
    return parse_credit_portfolio_pages(
        portfolio_pages,
        report_as_of=_month_end(report_year, report_month),
        published_at=published_at,
        document_digest=sha256(content).hexdigest(),
        document_url=_report_url(report_year, report_month),
    )


def parse_credit_portfolio_pages(
    pages: tuple[str, ...],
    *,
    report_as_of: date,
    published_at: datetime | None = None,
    document_digest: str | None = None,
    document_url: str | None = None,
) -> CreditPortfolio:
    """Require a contiguous issue inventory and bounded, reconciled weights."""

    holdings: list[CreditHolding] = []
    for page in pages:
        if len(_CREDIT_ROW_START.findall(page)) != len(_CREDIT_ROW.findall(page)):
            raise ValueError("Manager portfolio contains an unreadable issue row")
        for match in _CREDIT_ROW.finditer(page):
            number, code, issuer, rating, spread, duration, weight = match.groups()
            holding = CreditHolding(
                row_number=int(number),
                security_code=code,
                issuer_and_sector=" ".join(issuer.split()),
                disclosed_rating=rating,
                credit_spread=Decimal(spread.replace(",", ".")) / Decimal("100"),
                duration_years=Decimal(duration.replace(",", ".")),
                portfolio_weight=Decimal(weight.replace(",", ".")) / Decimal("100"),
            )
            if (
                not holding.issuer_and_sector
                or re.search(r"\n\d{1,4} Debêntures", issuer)
                or holding.duration_years < 0
                or not Decimal("0") <= holding.portfolio_weight <= Decimal("1")
                or holding.row_number != len(holdings) + 1
            ):
                raise ValueError("Manager portfolio row is inconsistent")
            holdings.append(holding)
    if len(holdings) < 30:
        raise ValueError("Manager portfolio inventory is incomplete")
    cash = _CASH_ROW.search(pages[-1])
    total = _PORTFOLIO_TOTAL.search(pages[-1])
    if cash is None or total is None or int(cash.group(1)) != len(holdings) + 1:
        raise ValueError("Manager portfolio lacks its final cash and total rows")
    cash_weight = Decimal(cash.group(2).replace(",", ".")) / Decimal("100")
    total_weight = Decimal(total.group(1).replace(",", ".")) / Decimal("100")
    if total_weight != 1 or not Decimal("0") <= cash_weight <= 1:
        raise ValueError("Manager portfolio total is inconsistent")
    portfolio = CreditPortfolio(
        report_as_of,
        published_at,
        tuple(holdings),
        cash_weight,
        document_digest,
        document_url,
    )
    # A displayed one-decimal percentage can differ from its exact weight by
    # at most 0.05 percentage points per issue. Keep that rounding allowance.
    rounding_allowance = Decimal(len(holdings) + 1) * Decimal("0.0005")
    if abs(portfolio.reported_weight + cash_weight - total_weight) > rounding_allowance:
        raise ValueError("Manager portfolio weights do not reconcile to total assets")
    return portfolio


class SpartaDistributionProvider:
    """Fetch one bounded official PDF and share the result across requests."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport
        self._cached_key: tuple[int, int, date] | None = None
        self._cached_until = 0.0
        self._cached_value: tuple[FundMonthlyDistribution, ...] = ()
        self._cached_document: bytes | None = None
        self._cached_portfolio: CreditPortfolio | None = None
        self._lock = asyncio.Lock()

    async def credit_portfolio(self, *, as_of: date | datetime | None = None) -> CreditPortfolio:
        """Reuse the income report document for its issue-level inventory."""

        monthly = await self.distributions(as_of=as_of)
        async with self._lock:
            if self._cached_value != monthly or self._cached_document is None:
                raise ProviderUnavailableError(ticker="JURO11")
            if self._cached_portfolio is not None:
                return self._cached_portfolio
            content = self._cached_document
            report_as_of = monthly[-1].report_as_of
            published_at = monthly[-1].published_at
            try:
                portfolio = await asyncio.to_thread(
                    parse_credit_portfolio,
                    content,
                    report_year=report_as_of.year,
                    report_month=report_as_of.month,
                    published_at=published_at,
                )
            except ValueError as error:
                raise ProviderInvalidResponseError(ticker="JURO11") from error
            self._cached_portfolio = portfolio
            return portfolio

    async def distributions(
        self, *, as_of: date | datetime | None = None
    ) -> tuple[FundMonthlyDistribution, ...]:
        reference_time = (
            as_of.astimezone(UTC)
            if isinstance(as_of, datetime)
            else datetime.combine(as_of, datetime.max.time(), tzinfo=UTC)
            if isinstance(as_of, date)
            else datetime.now(UTC)
        )
        reference = reference_time.date()
        latest_closed = date(reference.year, reference.month, 1).toordinal() - 1
        latest = date.fromordinal(latest_closed)
        cache_key = (latest.year, latest.month, reference)
        async with self._lock:
            if (
                self._cached_key == cache_key
                and monotonic() < self._cached_until
                and (
                    as_of is None
                    or self._cached_value[0].published_at is not None
                    and self._cached_value[0].published_at <= reference_time
                )
            ):
                return self._cached_value
            for offset in range(3):
                candidate = date(latest.year, latest.month, 1)
                for _ in range(offset):
                    candidate = date(
                        candidate.year - (candidate.month == 1),
                        (candidate.month - 2) % 12 + 1,
                        1,
                    )
                try:
                    downloaded = await asyncio.wait_for(
                        self._download(candidate.year, candidate.month), timeout=20
                    )
                except TimeoutError as error:
                    raise ProviderUnavailableError(ticker="JURO11") from error
                if downloaded is None:
                    continue
                content, published_at = downloaded
                if as_of is not None and (published_at is None or published_at > reference_time):
                    continue
                try:
                    result = await asyncio.to_thread(
                        parse_monthly_distributions,
                        content,
                        report_year=candidate.year,
                        report_month=candidate.month,
                        published_at=published_at,
                    )
                except ValueError as error:
                    raise ProviderInvalidResponseError(ticker="JURO11") from error
                if result[-1].payment_date > reference:
                    continue
                self._cached_key = cache_key
                self._cached_until = monotonic() + 6 * 3600
                self._cached_value = result
                self._cached_document = content
                self._cached_portfolio = None
                return result
        raise ProviderUnavailableError(ticker="JURO11")

    async def _download(self, year: int, month: int) -> tuple[bytes, datetime | None] | None:
        url = _report_url(year, month)
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10.0),
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                async with client.stream("GET", url) as response:
                    if response.status_code == 404:
                        return None
                    if response.status_code != 200:
                        raise ProviderUnavailableError(ticker="JURO11")
                    modified = response.headers.get("last-modified")
                    try:
                        published_at = (
                            parsedate_to_datetime(modified).astimezone(UTC) if modified else None
                        )
                    except (TypeError, ValueError, OverflowError):
                        published_at = None
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > _MAX_PDF_BYTES:
                            raise ProviderInvalidResponseError(ticker="JURO11")
                        chunks.append(chunk)
                    return b"".join(chunks), published_at
        except httpx.HTTPError as error:
            raise ProviderUnavailableError(ticker="JURO11") from error
