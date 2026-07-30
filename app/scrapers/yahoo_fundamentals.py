"""Annual fundamentals for internationally listed companies.

Brazilian issuers file with the CVM, which is the source of every fundamental
this API publishes for them. Foreign issuers have no equivalent free bulk
filing source, so their statements are read from the public Yahoo Finance
fundamentals timeseries: it needs no API key, covers ordinary shares and REITs
alike, and reports the annual line items the quality methodology consumes.

Only observed figures are mapped. A line item the source does not report stays
absent so the consumer can redistribute its weight instead of scoring a guess.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.config import Settings
from app.models.fundamentals import FinancialPeriod, FundamentalsSnapshot

SOURCE_YAHOO = "yahoo"

# Statement lines requested from the timeseries endpoint, mapped onto the
# fields of ``FinancialPeriod``.
_ANNUAL_FIELDS: dict[str, str] = {
    "annualTotalRevenue": "revenue",
    "annualGrossProfit": "gross_profit",
    "annualEBIT": "ebit",
    "annualEBITDA": "ebitda",
    "annualNetIncome": "net_income",
    "annualStockholdersEquity": "equity",
    "annualTotalAssets": "total_assets",
    "annualCurrentAssets": "current_assets",
    "annualCurrentLiabilities": "current_liabilities",
    "annualCashAndCashEquivalents": "cash_and_equivalents",
    "annualTotalDebt": "gross_debt",
    "annualNetDebt": "net_debt",
    "annualOperatingCashFlow": "operating_cash_flow",
    "annualCapitalExpenditure": "capex",
    "annualFreeCashFlow": "free_cash_flow",
    "annualReconciledDepreciation": "depreciation",
    "annualInterestExpense": "financial_result",
    "annualOrdinarySharesNumber": "shares_outstanding",
}
# The endpoint rejects a window that starts at the epoch, so the range begins
# in 1985 and ends far enough ahead to always include the latest filing.
_PERIOD_START = "493590046"
_PERIOD_END = "1900000000"


class YahooFundamentalsProvider:
    """Reads annual statements for a foreign listing."""

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport

    async def snapshot(self, ticker: str) -> FundamentalsSnapshot | None:
        """Annual history for one ticker, or ``None`` when nothing is reported."""
        payload = await self._timeseries(ticker)
        if payload is None:
            return None
        return parse_yahoo_fundamentals(ticker, payload)

    async def _timeseries(self, ticker: str) -> dict[str, Any] | None:
        async with httpx.AsyncClient(
            base_url=self.settings.yahoo_fundamentals_base_url,
            timeout=httpx.Timeout(self.settings.request_timeout_seconds),
            transport=self.transport,
            headers={"User-Agent": self.settings.user_agent},
        ) as client:
            response = await client.get(
                f"/ws/fundamentals-timeseries/v1/finance/timeseries/{ticker}",
                params={
                    "symbol": ticker,
                    "type": ",".join(_ANNUAL_FIELDS),
                    "period1": _PERIOD_START,
                    "period2": _PERIOD_END,
                    "merge": "false",
                },
            )
        if response.status_code in {404, 401, 403}:
            return None
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else None


def parse_yahoo_fundamentals(
    ticker: str,
    payload: dict[str, Any],
) -> FundamentalsSnapshot | None:
    """Build annual periods from the timeseries payload.

    Each requested line arrives as its own series, so the values are collected
    per reporting date and only then turned into periods. A date reporting no
    usable line is dropped rather than published as an empty filing.
    """
    results = _results(payload)
    if not results:
        return None
    by_period: dict[date, dict[str, Decimal]] = {}
    for series in results:
        field = _series_field(series)
        if field is None:
            continue
        for observation in series.get(_series_type(series), []) or []:
            if not isinstance(observation, dict):
                continue
            period_end = _as_date(observation.get("asOfDate"))
            value = _as_decimal((observation.get("reportedValue") or {}).get("raw"))
            if period_end is None or value is None:
                continue
            by_period.setdefault(period_end, {})[field] = value
    periods = [
        FinancialPeriod(
            period_end=period_end,
            consolidated=True,
            annual=True,
            source=SOURCE_YAHOO,
            **_normalized(values),
        )
        for period_end, values in sorted(by_period.items())
        if values
    ]
    if not periods:
        return None
    latest = periods[-1]
    return FundamentalsSnapshot(
        ticker=ticker,
        periods=periods,
        trailing_twelve_months=latest,
        shares_outstanding=latest.shares_outstanding,
        earnings_per_share=_per_share(latest.net_income, latest.shares_outstanding),
        book_value_per_share=_per_share(latest.equity, latest.shares_outstanding),
    )


def _normalized(values: dict[str, Decimal]) -> dict[str, Decimal]:
    """Adjust the few lines whose sign convention differs from this model."""
    normalized = dict(values)
    # Yahoo reports interest expense as a positive cost, while ``financial_result``
    # is the signed contribution of the financial lines to the result.
    expense = normalized.get("financial_result")
    if expense is not None and expense > 0:
        normalized["financial_result"] = -expense
    # Capital expenditure arrives negative because it is a cash outflow; the
    # consumers of ``capex`` expect the invested amount.
    capex = normalized.get("capex")
    if capex is not None and capex < 0:
        normalized["capex"] = -capex
    return normalized


def _results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    timeseries = payload.get("timeseries")
    if not isinstance(timeseries, dict):
        return []
    results = timeseries.get("result")
    return [item for item in results if isinstance(item, dict)] if isinstance(results, list) else []


def _series_type(series: dict[str, Any]) -> str:
    meta = series.get("meta")
    if not isinstance(meta, dict):
        return ""
    types = meta.get("type")
    return str(types[0]) if isinstance(types, list) and types else ""


def _series_field(series: dict[str, Any]) -> str | None:
    return _ANNUAL_FIELDS.get(_series_type(series))


def _per_share(total: Decimal | None, shares: Decimal | None) -> Decimal | None:
    if total is None or shares is None or shares <= 0:
        return None
    return total / shares


def _as_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _as_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC).date()
    except ValueError:
        return None
