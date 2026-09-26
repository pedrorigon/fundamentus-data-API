from __future__ import annotations

import asyncio
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256

import httpx
import pytest
from pypdf.errors import PdfReadError

from app.core.errors import ProviderInvalidResponseError, ProviderUnavailableError
from app.models import FundMonthlyDistribution
from app.scrapers import sparta_fi_infra
from app.scrapers.sparta_fi_infra import (
    SpartaDistributionProvider,
    parse_credit_portfolio,
    parse_credit_portfolio_pages,
    parse_monthly_distributions,
)

_COVER = "RELATÓRIO MENSAL DE GESTÃO\nAgosto/2026\nJURO11\nCNPJ 42.730.834/0001-00\n"
_ROWS = """DISTRIBUIÇÕES DE RENDIMENTOS
Ago-26 15/09/2026 R$ 1,00 10,2%
Jul-26 14/08/2026 R$ 0,75 10,2%
Jun-26 15/07/2026 R$ 0,00 10,5%
Mai-26 15/06/2026 R$ 0,50 11,6%
Abr-26 15/05/2026 R$ 0,75 12,1%
Mar-26 15/04/2026 R$ 1,00 12,3%
Fev-26 13/03/2026 R$ 1,00 12,3%
Jan-26 13/02/2026 R$ 1,00 12,4%
Dez-25 15/01/2026 R$ 1,00 12,1%
Nov-25 12/12/2025 R$ 1,00 11,6%
Out-25 14/11/2025 R$ 1,00 11,6%
Set-25 14/10/2025 R$ 1,00 11,6%
"""


def _credit_page(*, rows: int = 30) -> str:
    return (
        "COMPOSIÇÃO DA CARTEIRA\n# TIPO CÓDIGO\n"
        + "".join(
            f"{number} Debêntures CODE{number:03} Issuer {number} Rodovias "
            f"{'S/R' if number == 2 else 'AAA'} 0,9% 6,6 1,0%\n"
            for number in range(1, rows + 1)
        )
        + f"{rows + 1} Caixa AAA 0,0% 0,0 70,0%\nTotal 0,0% 0,0 100,0%\n"
    )


class _Page:
    def __init__(self, content: str) -> None:
        self.content = content

    def extract_text(self) -> str:
        return self.content


def _reader(monkeypatch: pytest.MonkeyPatch, *, cover: str = _COVER, rows: str = _ROWS) -> None:
    class Reader:
        pages = [_Page(cover), _Page(""), _Page(""), _Page(rows)]

    monkeypatch.setattr(sparta_fi_infra, "PdfReader", lambda *_args, **_kwargs: Reader())


def test_official_report_keeps_explicit_zero_month(monkeypatch: pytest.MonkeyPatch) -> None:
    _reader(monkeypatch)

    periods = parse_monthly_distributions(b"%PDF-fixture", report_year=2026, report_month=8)

    assert len(periods) == 12
    assert sum((item.value for item in periods), Decimal("0")) == Decimal("10.00")
    assert periods[9].reference_month == date(2026, 6, 1)
    assert periods[9].payment_date == date(2026, 7, 15)
    assert periods[9].value == Decimal("0")


def test_manager_credit_inventory_is_reconciled_without_rating_to_default_mapping() -> None:
    portfolio = parse_credit_portfolio_pages((_credit_page(),), report_as_of=date(2026, 8, 31))

    assert len(portfolio.holdings) == 30
    assert portfolio.reported_weight == Decimal("0.30")
    assert portfolio.unrated_weight == Decimal("0.01")
    assert portfolio.cash_weight == Decimal("0.7")
    assert portfolio.holdings[0].security_code == "CODE001"
    assert portfolio.holdings[0].credit_spread == Decimal("0.009")
    assert portfolio.holdings[0].duration_years == Decimal("6.6")


@pytest.mark.parametrize(
    "page",
    [
        _credit_page(rows=29),
        _credit_page().replace("15 Debêntures", "16 Debêntures"),
        _credit_page().replace("1,0%", "9,0%"),
        _credit_page().replace("6,6 1,0%", "-6,6 1,0%"),
        _credit_page().replace("31 Caixa", "30 Caixa"),
        _credit_page().replace("Total 0,0% 0,0 100,0%", ""),
        _credit_page().replace("Total 0,0% 0,0 100,0%", "Total 0,0% 0,0 99,0%"),
        _credit_page().replace("Issuer 30 Rodovias AAA", "Issuer 30 Rodovias UNKNOWN"),
    ],
)
def test_credit_inventory_rejects_gaps_or_bad_weights(page: str) -> None:
    with pytest.raises(ValueError):
        parse_credit_portfolio_pages((page,), report_as_of=date(2026, 8, 31))


def test_credit_inventory_checks_document_identity_and_portfolio_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Reader:
        pages = [_Page(_COVER)] + [_Page("")] * 5 + [_Page(_credit_page())] * 9 + [_Page("")]

    monkeypatch.setattr(sparta_fi_infra, "PdfReader", lambda *_args, **_kwargs: Reader())
    with pytest.raises(ValueError):
        parse_credit_portfolio(b"%PDF-fixture", report_year=2026, report_month=7)


def test_credit_inventory_reads_table_pages_until_reconciled_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Reader:
        pages = (
            [_Page(_COVER)]
            + [_Page("")] * 5
            + [_Page(_credit_page())]
            + [_Page("COMPOSIÇÃO DA CARTEIRA\nALOCAÇÃO POR SETOR")]
        )

    monkeypatch.setattr(sparta_fi_infra, "PdfReader", lambda *_args, **_kwargs: Reader())
    result = parse_credit_portfolio(b"%PDF-fixture", report_year=2026, report_month=8)

    assert len(result.holdings) == 30
    assert result.cash_weight == Decimal("0.7")
    assert result.document_digest == sha256(b"%PDF-fixture").hexdigest()
    assert result.document_url == "https://sparta.com.br/uploads/JURO11_RelatorioMensal_2026_08.pdf"


def test_credit_inventory_rejects_invalid_or_unreadable_documents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="Invalid manager report document"):
        parse_credit_portfolio(b"not a PDF", report_year=2026, report_month=8)

    class ShortReader:
        pages = [_Page(_COVER)]

    monkeypatch.setattr(sparta_fi_infra, "PdfReader", lambda *_args, **_kwargs: ShortReader())
    with pytest.raises(ValueError, match="lacks its portfolio section"):
        parse_credit_portfolio(b"%PDF-fixture", report_year=2026, report_month=8)

    def broken_reader(*_args: object, **_kwargs: object) -> object:
        raise PdfReadError("invalid")

    monkeypatch.setattr(sparta_fi_infra, "PdfReader", broken_reader)
    with pytest.raises(ValueError, match="cannot be parsed"):
        parse_credit_portfolio(b"%PDF-fixture", report_year=2026, report_month=8)


@pytest.mark.parametrize(
    ("cover", "rows"),
    [
        (_COVER.replace("42.730.834/0001-00", "43.140.450/0001-92"), _ROWS),
        (_COVER, _ROWS.replace("Jun-26 15/07/2026 R$ 0,00 10,5%\n", "")),
        (_COVER, _ROWS + "Ago-26 15/09/2026 R$ 1,00 10,2%\n"),
    ],
)
def test_official_report_rejects_wrong_identity_or_incomplete_months(
    monkeypatch: pytest.MonkeyPatch, cover: str, rows: str
) -> None:
    _reader(monkeypatch, cover=cover, rows=rows)

    with pytest.raises(ValueError):
        parse_monthly_distributions(b"%PDF-fixture", report_year=2026, report_month=8)


def test_official_report_rejects_invalid_pdf_and_table_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="Invalid manager report document"):
        parse_monthly_distributions(b"not a PDF", report_year=2026, report_month=8)

    class ShortReader:
        pages = [_Page(_COVER)]

    monkeypatch.setattr(sparta_fi_infra, "PdfReader", lambda *_args, **_kwargs: ShortReader())
    with pytest.raises(ValueError, match="Unexpected manager report length"):
        parse_monthly_distributions(b"%PDF-fixture", report_year=2026, report_month=8)

    def broken_reader(*_args: object, **_kwargs: object) -> object:
        raise PdfReadError("invalid")

    monkeypatch.setattr(sparta_fi_infra, "PdfReader", broken_reader)
    with pytest.raises(ValueError, match="cannot be parsed"):
        parse_monthly_distributions(b"%PDF-fixture", report_year=2026, report_month=8)


@pytest.mark.parametrize(
    ("rows", "reason"),
    [
        (_ROWS.replace("15/07/2026", "31/02/2026"), "Invalid manager distribution row"),
        (
            _ROWS.replace("Jun-26 15/07/2026", "Ago-25 15/09/2025"),
            "months are not contiguous",
        ),
    ],
)
def test_official_report_rejects_invalid_dates_and_gaps(
    monkeypatch: pytest.MonkeyPatch, rows: str, reason: str
) -> None:
    _reader(monkeypatch, rows=rows)
    with pytest.raises(ValueError, match=reason):
        parse_monthly_distributions(b"%PDF-fixture", report_year=2026, report_month=8)


@pytest.mark.asyncio
async def test_provider_fetches_latest_closed_month_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _reader(monkeypatch)
    urls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(
            200,
            content=b"%PDF-fixture",
            headers={"Last-Modified": "Thu, 03 Sep 2026 18:24:42 GMT"},
        )

    provider = SpartaDistributionProvider(transport=httpx.MockTransport(respond))

    first = await provider.distributions(as_of=date(2026, 9, 25))
    second = await provider.distributions(as_of=date(2026, 9, 25))

    assert first == second
    assert urls == ["https://sparta.com.br/uploads/JURO11_RelatorioMensal_2026_08.pdf"]


@pytest.mark.asyncio
async def test_provider_reuses_report_for_credit_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    class Reader:
        pages = (
            [_Page(_COVER), _Page(""), _Page(""), _Page(_ROWS)]
            + [_Page("")] * 2
            + [_Page(_credit_page())]
            + [_Page("COMPOSIÇÃO DA CARTEIRA\nALOCAÇÃO POR SETOR")]
        )

    monkeypatch.setattr(sparta_fi_infra, "PdfReader", lambda *_args, **_kwargs: Reader())
    requests = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            content=b"%PDF-fixture",
            headers={"Last-Modified": "Thu, 03 Sep 2026 18:24:42 GMT"},
        )

    provider = SpartaDistributionProvider(transport=httpx.MockTransport(respond))
    first = await provider.credit_portfolio(as_of=date(2026, 9, 25))
    second = await provider.credit_portfolio(as_of=date(2026, 9, 25))

    assert first is second
    assert first.report_as_of == date(2026, 8, 31)
    assert requests == 1


@pytest.mark.asyncio
async def test_provider_reports_invalid_credit_section_separately_from_valid_income(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reader(monkeypatch)
    provider = SpartaDistributionProvider(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                content=b"%PDF-fixture",
                headers={"Last-Modified": "Thu, 03 Sep 2026 18:24:42 GMT"},
            )
        )
    )

    assert len(await provider.distributions(as_of=date(2026, 9, 25))) == 12
    with pytest.raises(ProviderInvalidResponseError):
        await provider.credit_portfolio(as_of=date(2026, 9, 25))


@pytest.mark.asyncio
async def test_provider_excludes_report_published_after_historical_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    urls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        modified = (
            "Thu, 03 Sep 2026 18:24:42 GMT"
            if "_08.pdf" in str(request.url)
            else "Wed, 05 Aug 2026 20:24:47 GMT"
        )
        return httpx.Response(200, content=b"%PDF-fixture", headers={"Last-Modified": modified})

    def parsed(
        _content: bytes,
        *,
        report_year: int,
        report_month: int,
        published_at: datetime | None,
    ) -> tuple[FundMonthlyDistribution, ...]:
        return (
            FundMonthlyDistribution(
                reference_month=date(report_year, report_month, 1),
                payment_date=date(report_year + (report_month == 12), report_month % 12 + 1, 15),
                value=Decimal("1"),
                report_as_of=date(report_year, report_month, 31),
                source="sparta_manager",
                published_at=published_at,
            ),
        )

    monkeypatch.setattr(sparta_fi_infra, "parse_monthly_distributions", parsed)
    provider = SpartaDistributionProvider(transport=httpx.MockTransport(respond))

    earlier = await provider.distributions(as_of=date(2026, 9, 1))
    unpaid = await provider.distributions(as_of=date(2026, 9, 10))
    later = await provider.distributions(as_of=date(2026, 9, 25))

    assert earlier[0].report_as_of == date(2026, 7, 31)
    assert unpaid[0].report_as_of == date(2026, 7, 31)
    assert later[0].report_as_of == date(2026, 8, 31)
    assert urls == [
        "https://sparta.com.br/uploads/JURO11_RelatorioMensal_2026_08.pdf",
        "https://sparta.com.br/uploads/JURO11_RelatorioMensal_2026_07.pdf",
        "https://sparta.com.br/uploads/JURO11_RelatorioMensal_2026_08.pdf",
        "https://sparta.com.br/uploads/JURO11_RelatorioMensal_2026_07.pdf",
        "https://sparta.com.br/uploads/JURO11_RelatorioMensal_2026_08.pdf",
    ]


@pytest.mark.asyncio
async def test_provider_rejects_oversized_pdf() -> None:
    provider = SpartaDistributionProvider(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                content=b"%PDF-" + b"x" * 5_000_000,
                headers={"Last-Modified": "Thu, 03 Sep 2026 18:24:42 GMT"},
            )
        )
    )

    with pytest.raises(ProviderInvalidResponseError):
        await provider.distributions(as_of=date(2026, 9, 25))


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 503])
async def test_provider_bounds_missing_or_unavailable_reports(status: int) -> None:
    requests = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(status)

    provider = SpartaDistributionProvider(transport=httpx.MockTransport(respond))

    with pytest.raises(ProviderUnavailableError):
        await provider.distributions(as_of=date(2026, 9, 25))
    assert requests == (3 if status == 404 else 1)


@pytest.mark.asyncio
async def test_provider_rejects_invalid_parsed_report(monkeypatch: pytest.MonkeyPatch) -> None:
    _reader(monkeypatch, rows=_ROWS.replace("Jun-26 15/07/2026 R$ 0,00 10,5%\n", ""))
    provider = SpartaDistributionProvider(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                content=b"%PDF-fixture",
                headers={"Last-Modified": "Thu, 03 Sep 2026 18:24:42 GMT"},
            )
        )
    )

    with pytest.raises(ProviderInvalidResponseError):
        await provider.distributions(as_of=date(2026, 9, 25))


@pytest.mark.asyncio
async def test_provider_rejects_missing_publication_time_for_historical_slot() -> None:
    provider = SpartaDistributionProvider(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=b"%PDF-fixture"))
    )

    with pytest.raises(ProviderUnavailableError):
        await provider.distributions(as_of=date(2026, 9, 25))


@pytest.mark.asyncio
async def test_provider_shares_one_official_download_across_concurrent_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reader(monkeypatch)
    requests = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            content=b"%PDF-fixture",
            headers={"Last-Modified": "Thu, 03 Sep 2026 18:24:42 GMT"},
        )

    provider = SpartaDistributionProvider(transport=httpx.MockTransport(respond))
    results = await asyncio.gather(
        *(provider.distributions(as_of=date(2026, 9, 25)) for _ in range(10))
    )

    assert requests == 1
    assert all(result == results[0] for result in results)
    assert sum((row.value for row in results[0]), Decimal("0")) == Decimal("10.00")
