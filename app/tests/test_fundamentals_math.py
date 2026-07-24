from __future__ import annotations

from datetime import date
from decimal import Decimal

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
    StatementPeriod,
)
from app.services.fundamentals_math import (
    build_period,
    enterprise_value,
    per_share,
    ratio,
    trailing_twelve_months,
)


def statement(
    accounts: dict[str, Decimal],
    *,
    start: date | None = date(2024, 1, 1),
    end: date = date(2024, 12, 31),
    depreciation: Decimal | None = None,
) -> StatementPeriod:
    return StatementPeriod(
        cnpj="11111111000111",
        cvm_code="001",
        company_name="EMPRESA TESTE S.A.",
        reference_date=end,
        period_start=start,
        period_end=end,
        consolidated=True,
        accounts=accounts,
        depreciation=depreciation,
    )


def test_derives_ebitda_from_ebit_and_depreciation() -> None:
    period = build_period(statement({ACCOUNT_EBIT: Decimal("100")}, depreciation=Decimal("40")))

    assert period.ebitda == Decimal("140")


def test_ebitda_is_missing_without_ebit_and_depreciation() -> None:
    assert build_period(statement({})).ebitda is None


def test_net_debt_subtracts_cash_and_short_term_investments() -> None:
    period = build_period(
        statement(
            {
                ACCOUNT_CURRENT_DEBT: Decimal("50"),
                ACCOUNT_LONG_TERM_DEBT: Decimal("150"),
                ACCOUNT_CASH: Decimal("30"),
                ACCOUNT_SHORT_TERM_INVESTMENTS: Decimal("20"),
            }
        )
    )

    assert period.gross_debt == Decimal("200")
    assert period.net_debt == Decimal("150")


def test_net_cash_position_produces_negative_net_debt() -> None:
    period = build_period(
        statement({ACCOUNT_CURRENT_DEBT: Decimal("10"), ACCOUNT_CASH: Decimal("90")})
    )

    assert period.net_debt == Decimal("-80")


def test_net_debt_is_missing_without_debt_accounts() -> None:
    assert build_period(statement({ACCOUNT_CASH: Decimal("90")})).net_debt is None


def test_free_cash_flow_subtracts_capex_from_operating_cash_flow() -> None:
    period = build_period(
        statement(
            {
                ACCOUNT_OPERATING_CASH_FLOW: Decimal("300"),
                ACCOUNT_INVESTING_CASH_FLOW: Decimal("-120"),
            }
        )
    )

    assert period.capex == Decimal("120")
    assert period.free_cash_flow == Decimal("180")


def test_positive_investing_flow_is_not_treated_as_capex() -> None:
    period = build_period(
        statement(
            {
                ACCOUNT_OPERATING_CASH_FLOW: Decimal("300"),
                ACCOUNT_INVESTING_CASH_FLOW: Decimal("50"),
            }
        )
    )

    assert period.capex == Decimal("0")
    assert period.free_cash_flow == Decimal("300")


def test_period_shorter_than_a_year_is_quarterly() -> None:
    quarterly = build_period(statement({}, start=date(2024, 1, 1), end=date(2024, 3, 31)))
    annual = build_period(statement({}))

    assert quarterly.annual is False
    assert annual.annual is True


def test_missing_start_date_is_treated_as_annual() -> None:
    assert build_period(statement({}, start=None)).annual is True


def quarter(end: date, revenue: str, equity: str = "1000") -> FinancialPeriod:
    return FinancialPeriod(
        period_end=end,
        consolidated=True,
        annual=False,
        revenue=Decimal(revenue),
        net_income=Decimal(revenue) / Decimal("10"),
        equity=Decimal(equity),
    )


def test_trailing_twelve_months_sums_four_quarters() -> None:
    periods = [
        quarter(date(2024, 3, 31), "100"),
        quarter(date(2024, 6, 30), "110"),
        quarter(date(2024, 9, 30), "120"),
        quarter(date(2024, 12, 31), "130", equity="1500"),
    ]

    ttm = trailing_twelve_months(periods)

    assert ttm is not None
    assert ttm.revenue == Decimal("460")
    # Stock accounts come from the latest period rather than being summed.
    assert ttm.equity == Decimal("1500")
    assert ttm.annual is True


def test_trailing_twelve_months_needs_four_quarters() -> None:
    periods = [quarter(date(2024, 3, 31), "100"), quarter(date(2024, 6, 30), "110")]

    assert trailing_twelve_months(periods) is None


def test_trailing_twelve_months_prefers_latest_annual_period() -> None:
    annual = FinancialPeriod(
        period_end=date(2024, 12, 31),
        consolidated=True,
        annual=True,
        revenue=Decimal("500"),
    )

    assert trailing_twelve_months([annual]) is annual


def test_trailing_twelve_months_returns_none_without_periods() -> None:
    assert trailing_twelve_months([]) is None


def test_partial_quarter_data_does_not_produce_partial_sum() -> None:
    periods = [
        quarter(date(2024, 3, 31), "100"),
        quarter(date(2024, 6, 30), "110"),
        quarter(date(2024, 9, 30), "120"),
        FinancialPeriod(period_end=date(2024, 12, 31), consolidated=True, annual=False),
    ]

    ttm = trailing_twelve_months(periods)

    assert ttm is not None
    assert ttm.revenue is None


def test_ratio_rejects_non_positive_denominators() -> None:
    assert ratio(Decimal("10"), Decimal("0")) is None
    assert ratio(Decimal("10"), Decimal("-5")) is None
    assert ratio(None, Decimal("5")) is None
    assert ratio(Decimal("10"), Decimal("5")) == Decimal("2")


def test_per_share_requires_positive_share_count() -> None:
    assert per_share(Decimal("100"), Decimal("0")) is None
    assert per_share(Decimal("100"), None) is None
    assert per_share(Decimal("100"), Decimal("4")) == Decimal("25")


def test_enterprise_value_adds_net_debt() -> None:
    assert enterprise_value(Decimal("1000"), Decimal("200")) == Decimal("1200")
    assert enterprise_value(Decimal("1000"), None) == Decimal("1000")
    assert enterprise_value(None, Decimal("200")) is None


def test_build_period_maps_core_accounts() -> None:
    period = build_period(
        statement(
            {
                ACCOUNT_REVENUE: Decimal("1000"),
                ACCOUNT_NET_INCOME: Decimal("150"),
                ACCOUNT_EQUITY: Decimal("900"),
            }
        ),
        shares_outstanding=Decimal("100"),
    )

    assert period.revenue == Decimal("1000")
    assert period.net_income == Decimal("150")
    assert period.equity == Decimal("900")
    assert period.shares_outstanding == Decimal("100")
