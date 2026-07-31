"""Multi-year statements for internationally listed companies and REITs.

Brazilian issuers file with the CVM, which is the source of every fundamental
this API publishes for them. Foreign issuers have no equivalent bulk filing
source, so their statements are read from the public pages that publish them.

Two pages are combined because neither is sufficient on its own:

* the financials page carries the annual income statement for the last five
  years, embedded in the page as parallel arrays. The rendered tables on the
  same site are filled in by the browser and arrive truncated on roughly two
  out of three requests, so the embedded arrays are read instead: they are
  present on every response and carry full precision.
* the Status Invest page carries the balance sheet as a single stable snapshot.
  It publishes ordinary shares only, so a REIT resolves its income statement
  and reports the balance sheet as unavailable rather than borrowing another
  issuer's figures.

Only values a page actually states are mapped. A line neither page publishes
stays absent so the consumer can redistribute its weight instead of scoring a
guess.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import httpx
from selectolax.parser import HTMLParser

from app.config import Settings

SOURCE_STATEMENTS = "public_filings"

# Series embedded in the financials page, keyed by the identifier it uses.
_INCOME_SERIES: dict[str, str] = {
    "revenue": "revenue",
    "gp": "gross_profit",
    "opinc": "ebit",
    "netinccmn": "net_income",
}
# Balance-sheet rows, keyed by the label printed beside each value.
_BALANCE_LABELS: dict[str, str] = {
    "ativos": "total_assets",
    "ativo circulante": "current_assets",
    "divida bruta": "gross_debt",
    "divida liquida": "net_debt",
    "disponibilidade": "cash_and_equivalents",
    "patrimonio liquido": "equity",
}
_MAX_YEARS = 5


@dataclass(frozen=True)
class AnnualFigures:
    """Reported figures for one fiscal year."""

    period_end: date
    revenue: Decimal | None = None
    gross_profit: Decimal | None = None
    ebit: Decimal | None = None
    net_income: Decimal | None = None
    earnings_per_share: Decimal | None = None
    operating_cash_flow: Decimal | None = None


@dataclass(frozen=True)
class InternationalStatements:
    """Everything two public pages state about one foreign listing."""

    ticker: str
    years: tuple[AnnualFigures, ...] = field(default_factory=tuple)
    equity: Decimal | None = None
    total_assets: Decimal | None = None
    current_assets: Decimal | None = None
    gross_debt: Decimal | None = None
    net_debt: Decimal | None = None
    cash_and_equivalents: Decimal | None = None
    market_capitalization: Decimal | None = None
    currency: str = "USD"
    source: str = SOURCE_STATEMENTS

    @property
    def is_available(self) -> bool:
        return bool(self.years)


class InternationalStatementsProvider:
    """Reads the public statement pages of a foreign listing."""

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport

    async def statements(self, ticker: str) -> InternationalStatements | None:
        """Annual statements for one ticker, or ``None`` when none are published."""
        income = await self._income_page(ticker)
        if income is None:
            return None
        years = parse_annual_income(income)
        if not years:
            return None
        years = _with_operating_cash_flow(
            years,
            parse_operating_cash_flow(await self._cash_flow_page(ticker) or ""),
        )
        balance = parse_balance_sheet(await self._balance_page(ticker) or "")
        return InternationalStatements(
            ticker=ticker.upper(),
            years=years,
            equity=balance.get("equity"),
            total_assets=balance.get("total_assets"),
            current_assets=balance.get("current_assets"),
            gross_debt=balance.get("gross_debt"),
            net_debt=balance.get("net_debt"),
            cash_and_equivalents=balance.get("cash_and_equivalents"),
            market_capitalization=balance.get("market_capitalization"),
        )

    async def _income_page(self, ticker: str) -> str | None:
        return await self._get(
            self.settings.stock_analysis_base_url,
            f"/stocks/{ticker.lower()}/financials/",
        )

    async def _cash_flow_page(self, ticker: str) -> str | None:
        # The full statement is filled in by the browser; this range renders it
        # server-side, which is why it is requested explicitly.
        return await self._get(
            self.settings.stock_analysis_base_url,
            f"/stocks/{ticker.lower()}/financials/cash-flow-statement/?range=10Y",
        )

    async def _balance_page(self, ticker: str) -> str | None:
        return await self._get(
            self.settings.status_invest_base_url,
            f"/acoes/eua/{ticker.upper()}",
        )

    async def _get(self, base_url: str, path: str) -> str | None:
        async with httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(self.settings.request_timeout_seconds),
            transport=self.transport,
            headers={"User-Agent": self.settings.user_agent},
            follow_redirects=True,
        ) as client:
            response = await client.get(path)
        return response.text if response.status_code == 200 else None


def parse_annual_income(html: str) -> tuple[AnnualFigures, ...]:
    """Read the annual income statement embedded in the financials page.

    The page declares one array per line item alongside the reporting dates, so
    the arrays are read in parallel and zipped back into years, most recent
    first on the page and oldest first in the result.
    """
    block = _embedded_block(html)
    if block is None:
        return ()
    period_ends = [
        parsed
        for value in _string_array(block, "datekey")
        if (parsed := _as_date(value)) is not None
    ]
    if not period_ends:
        return ()
    series = {field_name: _decimal_array(block, key) for key, field_name in _INCOME_SERIES.items()}
    per_share = _decimal_array(block, "epsdil")
    years = [
        AnnualFigures(
            period_end=period_end,
            revenue=_at(series["revenue"], index),
            gross_profit=_at(series["gross_profit"], index),
            ebit=_at(series["ebit"], index),
            net_income=_at(series["net_income"], index),
            earnings_per_share=_at(per_share, index),
        )
        for index, period_end in enumerate(period_ends[:_MAX_YEARS])
    ]
    return tuple(reversed(years))


def parse_operating_cash_flow(html: str) -> dict[date, Decimal]:
    """Operating cash flow per year, from the rendered cash-flow table.

    Only this line is taken. The rest of the statement renders inconsistently
    across requests, and a line present on one response and missing on the next
    would make the score depend on which response arrived.
    """
    if not html:
        return {}
    table = HTMLParser(html).css_first("table")
    if table is None:
        return {}
    period_ends = _table_period_ends(table)
    values = _table_row(table, "operating cash flow")
    return {
        period_end: value
        for period_end, value in zip(period_ends, values, strict=False)
        if value is not None and period_end != date.min
    }


def _with_operating_cash_flow(
    years: tuple[AnnualFigures, ...],
    cash_flows: dict[date, Decimal],
) -> tuple[AnnualFigures, ...]:
    if not cash_flows:
        return years
    return tuple(
        replace(year, operating_cash_flow=cash_flows.get(year.period_end)) for year in years
    )


def _table_period_ends(table: object) -> list[date]:
    """Column dates, read from the period header of the statement table."""
    for row in table.css("tr"):  # type: ignore[attr-defined]
        cells = [" ".join(cell.text().split()) for cell in row.css("th,td")]
        if cells and _key(cells[0]) == "period ending":
            return [_column_date(cell) for cell in cells[1:]]
    return []


def _table_row(table: object, label: str) -> list[Decimal | None]:
    for row in table.css("tr"):  # type: ignore[attr-defined]
        cells = row.css("th,td")
        if cells and _key(cells[0].text()) == label:
            return [_scaled_million(" ".join(cell.text().split())) for cell in cells[1:]]
    return []


def _column_date(text: str) -> date:
    match = re.search(r"([A-Z][a-z]{2}) (\d{1,2}), (\d{4})", text)
    if match is None:
        return date.min
    try:
        return datetime.strptime(" ".join(match.groups()), "%b %d %Y").date()
    except ValueError:
        return date.min


def _scaled_million(text: str) -> Decimal | None:
    """Statement tables are stated in millions."""
    value = _as_decimal(text.replace(",", ""))
    return None if value is None else value * Decimal("1000000")


def parse_balance_sheet(html: str) -> dict[str, Decimal]:
    """Read the balance-sheet totals stated beside their labels."""
    values: dict[str, Decimal] = {}
    for label, field_name in _BALANCE_LABELS.items():
        parsed = _labelled_value(html, label)
        if parsed is not None:
            values[field_name] = parsed
    market_cap = _labelled_value(html, "valor de mercado")
    if market_cap is not None:
        values["market_capitalization"] = market_cap
    return values


def _embedded_block(html: str) -> str | None:
    start = html.find("data:{datekey")
    return None if start < 0 else html[start : start + 4000]


def _string_array(block: str, key: str) -> list[str]:
    match = re.search(rf"\b{key}:\[([^\]]*)\]", block)
    if match is None:
        return []
    return [item.strip().strip('"') for item in match.group(1).split(",") if item.strip()]


def _decimal_array(block: str, key: str) -> list[Decimal | None]:
    return [_as_decimal(item) for item in _string_array(block, key)]


def _labelled_value(html: str, label: str) -> Decimal | None:
    """Value of the element that follows a printed indicator title."""
    for match in re.finditer(r"<h3[^>]*>([^<]+)</h3>", html):
        if _key(match.group(1)) != label:
            continue
        tail = html[match.end() : match.end() + 900]
        value = re.search(r'class="[^"]*\bvalue\b[^"]*"[^>]*>([^<]+)<', tail)
        if value is not None:
            parsed = _as_decimal(" ".join(value.group(1).split()))
            if parsed is not None:
                return parsed
    return None


def _at(values: list[Decimal | None], index: int) -> Decimal | None:
    return values[index] if index < len(values) else None


def _key(label: str) -> str:
    folded = unicodedata.normalize("NFKD", " ".join(label.split()))
    return folded.encode("ascii", "ignore").decode("ascii").strip().lower()


def _as_decimal(value: str) -> Decimal | None:
    text = (value or "").strip()
    if not text or text in {"-", "--"}:
        return None
    # Brazilian pages group thousands with dots; the embedded arrays do not.
    normalized = text.replace(".", "").replace(",", ".") if "," in text else text
    try:
        return Decimal(normalized)
    except InvalidOperation:
        return None


def _as_date(value: str) -> date | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None
