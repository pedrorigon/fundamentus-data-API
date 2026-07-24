from __future__ import annotations

from dataclasses import replace
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
    is_financial_sector,
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
    published_at: date | None = date(2025, 3, 26),
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
        published_at=published_at,
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


def interim(end: date, revenue: str, equity: str = "1000") -> FinancialPeriod:
    return FinancialPeriod(
        period_end=end,
        published_at=end,
        consolidated=True,
        annual=False,
        revenue=Decimal(revenue),
        net_income=Decimal(revenue) / Decimal("10"),
        equity=Decimal(equity),
    )


def test_trailing_twelve_months_adjusts_cumulative_interim_flows() -> None:
    periods = [
        interim(date(2023, 9, 30), "900"),
        FinancialPeriod(
            period_end=date(2023, 12, 31),
            published_at=date(2024, 3, 20),
            consolidated=True,
            annual=True,
            revenue=Decimal("1300"),
            net_income=Decimal("130"),
        ),
        interim(date(2024, 9, 30), "1200", equity="1500"),
    ]

    ttm = trailing_twelve_months(periods)

    assert ttm is not None
    assert ttm.revenue == Decimal("1600")
    assert ttm.net_income == Decimal("160")
    # Stock accounts come from the latest period rather than being summed.
    assert ttm.equity == Decimal("1500")
    assert ttm.annual is True


def test_trailing_twelve_months_needs_prior_annual_and_comparable_interim() -> None:
    periods = [interim(date(2024, 3, 31), "100"), interim(date(2024, 6, 30), "110")]

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
        interim(date(2023, 9, 30), "900"),
        FinancialPeriod(
            period_end=date(2023, 12, 31),
            consolidated=True,
            annual=True,
            revenue=Decimal("1300"),
        ),
        FinancialPeriod(
            period_end=date(2024, 9, 30),
            consolidated=True,
            annual=False,
        ),
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


def test_build_period_derives_historical_shares_from_filing_eps() -> None:
    period = build_period(
        statement(
            {
                ACCOUNT_NET_INCOME: Decimal("150"),
                "3.99.01.01": Decimal("3"),
            }
        )
    )

    assert period.shares_outstanding == Decimal("50")


def test_build_period_uses_semantic_accounts_for_a_bank() -> None:
    source = statement(
        {
            ACCOUNT_EBIT: Decimal("500"),
            ACCOUNT_EQUITY: Decimal("2400"),
            "2.08": Decimal("220"),
            "2.08.09": Decimal("10"),
            "3.09.01": Decimal("42"),
        }
    )
    source = replace(
        source,
        account_labels={
            ACCOUNT_EQUITY: "Passivos Financeiros ao Custo Amortizado",
            "2.08": "Patrimônio Líquido Consolidado",
            "2.08.09": "Participação dos Acionistas Não Controladores",
            "3.09.01": "Atribuído a Sócios da Empresa Controladora",
        },
    )

    period = build_period(source, sector="Bancos")

    assert period.net_income == Decimal("42")
    assert period.equity == Decimal("210")
    assert period.ebit is None
    assert period.ebitda is None
    assert period.free_cash_flow is None
    assert period.net_debt is None


def test_financial_sector_detection_is_accent_insensitive() -> None:
    assert is_financial_sector("Intermediários Financeiros") is True
    assert is_financial_sector("Seguros e Previdência") is True
    assert is_financial_sector("Bens Industriais") is False
