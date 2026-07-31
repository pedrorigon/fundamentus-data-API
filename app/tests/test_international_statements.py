"""Statements for foreign listings must be read exactly as published."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx
import pytest

from app.config import Settings
from app.scrapers.international_statements import (
    InternationalStatementsProvider,
    parse_annual_income,
    parse_balance_sheet,
    parse_operating_cash_flow,
)


def _income_page(
    *,
    datekey: str = '"2025-09-27","2024-09-28"',
    revenue: str = "416161000000,391035000000",
    extra: str = "",
) -> str:
    return (
        "<html><script>const config={"
        f"data:{{datekey:[{datekey}],revenue:[{revenue}],"
        "gp:[195201000000,180683000000],opinc:[133050000000,123216000000],"
        "netinccmn:[112010000000,93736000000],epsdil:[7.46,6.08]"
        f"{extra}}}}};</script></html>"
    )


def _balance_row(label: str, value: str) -> str:
    return f'<h3 class="title">{label}</h3><strong class="value">{value}</strong>'


def test_each_reported_year_becomes_a_period() -> None:
    years = parse_annual_income(_income_page())

    assert [year.period_end for year in years] == [date(2024, 9, 28), date(2025, 9, 27)]


def test_the_line_items_are_read_in_parallel() -> None:
    """Each statement line is its own array, indexed by the same position."""
    years = parse_annual_income(_income_page())

    latest = years[-1]
    assert latest.revenue == Decimal("416161000000")
    assert latest.gross_profit == Decimal("195201000000")
    assert latest.ebit == Decimal("133050000000")
    assert latest.net_income == Decimal("112010000000")
    assert latest.earnings_per_share == Decimal("7.46")


def test_years_are_ordered_from_oldest_to_newest() -> None:
    years = parse_annual_income(_income_page())

    assert years[0].period_end < years[-1].period_end


def test_history_is_bounded_to_the_recent_years() -> None:
    dates = ",".join(f'"{2016 + index}-12-31"' for index in range(10))
    revenues = ",".join(str(index) for index in range(1, 11))

    years = parse_annual_income(_income_page(datekey=dates, revenue=revenues))

    assert len(years) == 5


def test_a_line_the_page_omits_stays_absent() -> None:
    """A shorter array must not shift the values of another year."""
    years = parse_annual_income(_income_page(revenue="416161000000"))

    assert years[-1].revenue == Decimal("416161000000")
    assert years[0].revenue is None


@pytest.mark.parametrize(
    "html",
    ["", "<html></html>", "<html>data:{datekey:[]}</html>"],
)
def test_a_page_without_the_embedded_series_reports_no_years(html: str) -> None:
    assert parse_annual_income(html) == ()


def test_an_unparsable_date_is_skipped() -> None:
    assert parse_annual_income(_income_page(datekey='"not-a-date"')) == ()


def test_balance_sheet_totals_are_read_beside_their_labels() -> None:
    html = "".join(
        [
            _balance_row("Ativos", "371.082.000.000,00"),
            _balance_row("Dívida bruta", "84.711.000.000,00"),
            _balance_row("Dívida líquida", "16.204.000.000,00"),
            _balance_row("Disponibilidade", "68.507.000.000,00"),
        ]
    )

    values = parse_balance_sheet(html)

    assert values["total_assets"] == Decimal("371082000000.00")
    assert values["gross_debt"] == Decimal("84711000000.00")
    assert values["net_debt"] == Decimal("16204000000.00")
    assert values["cash_and_equivalents"] == Decimal("68507000000.00")


def test_a_negative_net_debt_is_preserved() -> None:
    """Net cash is a real reading, not a missing value."""
    values = parse_balance_sheet(_balance_row("Dívida líquida", "-38.010.000.000,00"))

    assert values["net_debt"] == Decimal("-38010000000.00")


def test_labels_match_regardless_of_accents_and_spacing() -> None:
    values = parse_balance_sheet(_balance_row("Patrimonio  Liquido", "106.491.000.000,00"))

    assert values["equity"] == Decimal("106491000000.00")


def test_an_absent_balance_sheet_yields_no_values() -> None:
    assert parse_balance_sheet("<html></html>") == {}


def test_a_row_without_a_usable_value_is_skipped() -> None:
    assert parse_balance_sheet(_balance_row("Ativos", "-")) == {}


async def test_provider_combines_both_public_pages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "stockanalysis" in request.url.host:
            return httpx.Response(200, text=_income_page())
        return httpx.Response(200, text=_balance_row("Ativos", "371.082.000.000,00"))

    provider = InternationalStatementsProvider(
        Settings(),
        transport=httpx.MockTransport(handler),
    )

    statements = await provider.statements("AAPL")

    assert statements is not None
    assert statements.ticker == "AAPL"
    assert len(statements.years) == 2
    assert statements.total_assets == Decimal("371082000000.00")


async def test_a_listing_without_an_income_statement_is_absent() -> None:
    provider = InternationalStatementsProvider(
        Settings(),
        transport=httpx.MockTransport(lambda _request: httpx.Response(404)),
    )

    assert await provider.statements("NOPE") is None


async def test_an_unlisted_balance_sheet_does_not_block_the_income_statement() -> None:
    """A REIT resolves its statements even where the balance page has no entry."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "stockanalysis" in request.url.host:
            return httpx.Response(200, text=_income_page())
        return httpx.Response(404)

    provider = InternationalStatementsProvider(
        Settings(),
        transport=httpx.MockTransport(handler),
    )

    statements = await provider.statements("O")

    assert statements is not None
    assert statements.is_available
    assert statements.total_assets is None


def _cash_flow_table(label: str = "Operating Cash Flow") -> str:
    return (
        "<table><tr><th>Period Ending</th><th>Sep '25 Sep 27, 2025</th>"
        "<th>Sep '24 Sep 28, 2024</th></tr>"
        f"<tr><td>{label}</td><td>111,482</td><td>118,254</td></tr></table>"
    )


def test_operating_cash_flow_is_read_per_reporting_date() -> None:
    """Statement tables are stated in millions."""
    values = parse_operating_cash_flow(_cash_flow_table())

    assert values[date(2025, 9, 27)] == Decimal("111482000000")
    assert values[date(2024, 9, 28)] == Decimal("118254000000")


def test_a_cash_flow_page_without_the_line_yields_nothing() -> None:
    """The line renders inconsistently, so its absence must not become a zero."""
    assert parse_operating_cash_flow(_cash_flow_table("Net Income")) == {}


@pytest.mark.parametrize("html", ["", "<html></html>", "<table></table>"])
def test_a_cash_flow_page_without_a_table_yields_nothing(html: str) -> None:
    assert parse_operating_cash_flow(html) == {}


def test_columns_without_a_readable_date_are_skipped() -> None:
    html = (
        "<table><tr><th>Period Ending</th><th>TTM</th></tr>"
        "<tr><td>Operating Cash Flow</td><td>146,724</td></tr></table>"
    )

    assert parse_operating_cash_flow(html) == {}


async def test_operating_cash_flow_reaches_the_reported_years() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "cash-flow" in request.url.path:
            return httpx.Response(200, text=_cash_flow_table())
        if "stockanalysis" in request.url.host:
            return httpx.Response(200, text=_income_page())
        return httpx.Response(404)

    provider = InternationalStatementsProvider(
        Settings(),
        transport=httpx.MockTransport(handler),
    )

    statements = await provider.statements("AAPL")

    assert statements is not None
    assert statements.years[-1].operating_cash_flow == Decimal("111482000000")
