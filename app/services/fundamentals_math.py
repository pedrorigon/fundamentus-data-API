"""Derive financial figures from raw CVM statement periods.

Kept free of I/O so the accounting rules stay independently testable.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import cast

from app.models.fundamentals import FinancialPeriod
from app.parsers.cvm_statements import (
    ACCOUNT_CASH,
    ACCOUNT_CURRENT_DEBT,
    ACCOUNT_EBIT,
    ACCOUNT_EQUITY,
    ACCOUNT_INVESTING_CASH_FLOW,
    ACCOUNT_LONG_TERM_DEBT,
    ACCOUNT_NET_INCOME,
    ACCOUNT_OPERATING_CASH_FLOW,
    ACCOUNT_REVENUE,
    ACCOUNT_SHORT_TERM_INVESTMENTS,
    ACCOUNT_TOTAL_ASSETS,
    StatementPeriod,
)

_ANNUAL_MINIMUM_DAYS = 300


def build_period(
    statement: StatementPeriod,
    *,
    shares_outstanding: Decimal | None = None,
) -> FinancialPeriod:
    """Convert a raw statement period into derived financial figures."""
    ebit = statement.account(ACCOUNT_EBIT)
    depreciation = statement.depreciation
    operating_cash_flow = statement.account(ACCOUNT_OPERATING_CASH_FLOW)
    capex = _capex(statement)
    cash = _cash_position(statement)
    gross_debt = _gross_debt(statement)

    return FinancialPeriod(
        period_end=statement.period_end,
        published_at=statement.published_at,
        consolidated=statement.consolidated,
        annual=_is_annual(statement),
        revenue=statement.account(ACCOUNT_REVENUE),
        ebit=ebit,
        ebitda=_sum_optional(ebit, depreciation),
        net_income=statement.account(ACCOUNT_NET_INCOME),
        equity=statement.account(ACCOUNT_EQUITY),
        total_assets=statement.account(ACCOUNT_TOTAL_ASSETS),
        cash_and_equivalents=cash,
        gross_debt=gross_debt,
        net_debt=_net_debt(gross_debt, cash),
        operating_cash_flow=operating_cash_flow,
        capex=capex,
        free_cash_flow=_free_cash_flow(operating_cash_flow, capex),
        depreciation=depreciation,
        shares_outstanding=shares_outstanding,
    )


def trailing_twelve_months(periods: list[FinancialPeriod]) -> FinancialPeriod | None:
    """Aggregate flow accounts over the last twelve months.

    CVM interim statements report year-to-date flows, not isolated quarters.
    The trailing value is therefore the latest year-to-date amount plus the
    previous annual amount minus the comparable prior-year interim amount.
    Stock accounts are taken from the latest period.
    """
    if not periods:
        return None
    ordered = sorted(periods, key=lambda period: period.period_end, reverse=True)
    latest = ordered[0]
    if latest.annual:
        return latest

    previous_annual = next(
        (period for period in ordered if period.annual and period.period_end < latest.period_end),
        None,
    )
    comparable = next(
        (
            period
            for period in ordered
            if not period.annual
            and period.period_end.year == latest.period_end.year - 1
            and (period.period_end.month, period.period_end.day)
            == (latest.period_end.month, latest.period_end.day)
        ),
        None,
    )
    if previous_annual is None or comparable is None:
        return None

    return FinancialPeriod(
        period_end=latest.period_end,
        published_at=_latest_publication(latest, previous_annual, comparable),
        consolidated=latest.consolidated,
        annual=True,
        revenue=_trailing_value(latest, previous_annual, comparable, "revenue"),
        ebit=_trailing_value(latest, previous_annual, comparable, "ebit"),
        ebitda=_trailing_value(latest, previous_annual, comparable, "ebitda"),
        net_income=_trailing_value(latest, previous_annual, comparable, "net_income"),
        equity=latest.equity,
        total_assets=latest.total_assets,
        cash_and_equivalents=latest.cash_and_equivalents,
        gross_debt=latest.gross_debt,
        net_debt=latest.net_debt,
        operating_cash_flow=_trailing_value(
            latest,
            previous_annual,
            comparable,
            "operating_cash_flow",
        ),
        capex=_trailing_value(latest, previous_annual, comparable, "capex"),
        free_cash_flow=_trailing_value(
            latest,
            previous_annual,
            comparable,
            "free_cash_flow",
        ),
        depreciation=_trailing_value(
            latest,
            previous_annual,
            comparable,
            "depreciation",
        ),
        shares_outstanding=latest.shares_outstanding,
        source=latest.source,
    )


def per_share(value: Decimal | None, shares: Decimal | None) -> Decimal | None:
    if value is None or shares is None or shares <= 0:
        return None
    return value / shares


def enterprise_value(market_cap: Decimal | None, net_debt: Decimal | None) -> Decimal | None:
    if market_cap is None:
        return None
    return market_cap + (net_debt or Decimal("0"))


def ratio(numerator: Decimal | None, denominator: Decimal | None) -> Decimal | None:
    """Divide guarding against missing values and non-positive denominators.

    Non-positive denominators are rejected because the resulting multiples
    (negative earnings, negative EBITDA) are not comparable on a ranked scale.
    """
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


def _is_annual(statement: StatementPeriod) -> bool:
    if statement.period_start is None:
        return True
    return (statement.period_end - statement.period_start).days >= _ANNUAL_MINIMUM_DAYS


def _capex(statement: StatementPeriod) -> Decimal | None:
    """Approximate capex from the investing cash-flow total.

    CVM sub-codes for fixed-asset purchases vary by company, so the investing
    total is used as a stable proxy and reported as a positive outflow.
    """
    investing = statement.account(ACCOUNT_INVESTING_CASH_FLOW)
    if investing is None:
        return None
    return abs(investing) if investing < 0 else Decimal("0")


def _cash_position(statement: StatementPeriod) -> Decimal | None:
    cash = statement.account(ACCOUNT_CASH)
    investments = statement.account(ACCOUNT_SHORT_TERM_INVESTMENTS)
    return _sum_optional(cash, investments)


def _gross_debt(statement: StatementPeriod) -> Decimal | None:
    return _sum_optional(
        statement.account(ACCOUNT_CURRENT_DEBT),
        statement.account(ACCOUNT_LONG_TERM_DEBT),
    )


def _net_debt(gross_debt: Decimal | None, cash: Decimal | None) -> Decimal | None:
    if gross_debt is None:
        return None
    return gross_debt - (cash or Decimal("0"))


def _free_cash_flow(
    operating_cash_flow: Decimal | None,
    capex: Decimal | None,
) -> Decimal | None:
    if operating_cash_flow is None:
        return None
    return operating_cash_flow - (capex or Decimal("0"))


def _sum_optional(first: Decimal | None, second: Decimal | None) -> Decimal | None:
    if first is None and second is None:
        return None
    return (first or Decimal("0")) + (second or Decimal("0"))


def _trailing_value(
    latest: FinancialPeriod,
    previous_annual: FinancialPeriod,
    comparable: FinancialPeriod,
    attribute: str,
) -> Decimal | None:
    values = (
        getattr(latest, attribute),
        getattr(previous_annual, attribute),
        getattr(comparable, attribute),
    )
    if any(value is None for value in values):
        return None
    latest_value, annual_value, comparable_value = values
    assert latest_value is not None
    assert annual_value is not None
    assert comparable_value is not None
    return cast(Decimal, latest_value + annual_value - comparable_value)


def _latest_publication(*periods: FinancialPeriod) -> date | None:
    dates = [period.published_at for period in periods]
    if any(value is None for value in dates):
        return None
    return max(value for value in dates if value is not None)
