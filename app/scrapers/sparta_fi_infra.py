"""Verified monthly income observations from the JURO11 manager report."""

from __future__ import annotations

import asyncio
import re
from calendar import monthrange
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
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


def _month_end(year: int, month: int) -> date:
    return date(year, month, monthrange(year, month)[1])


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
    expected_period = f"{_MONTH_NAMES[report_month - 1]}/{report_year}"
    if (
        "JURO11" not in cover
        or expected_period not in cover
        or not any(
            re.sub(r"\D", "", match) == _CNPJ for match in re.findall(r"CNPJ\s+([\d./-]+)", cover)
        )
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


class SpartaDistributionProvider:
    """Fetch one bounded official PDF and share the result across requests."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport
        self._cached_key: tuple[int, int, date] | None = None
        self._cached_until = 0.0
        self._cached_value: tuple[FundMonthlyDistribution, ...] = ()
        self._lock = asyncio.Lock()

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
                return result
        raise ProviderUnavailableError(ticker="JURO11")

    async def _download(self, year: int, month: int) -> tuple[bytes, datetime | None] | None:
        url = f"https://sparta.com.br/uploads/JURO11_RelatorioMensal_{year}_{month:02d}.pdf"
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
