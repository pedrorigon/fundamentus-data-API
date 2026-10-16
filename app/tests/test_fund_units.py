from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.models import FundDistribution, FundMonthlyReport
from app.services.fund_units import normalize_fund_units
from app.services.quality import (
    _distribution_cut_frequency,
    _distribution_growth,
    _distribution_periods,
    _issuance_nav_preservation,
    _nav_growth,
    _nav_max_drawdown,
    _nav_return_observations,
)


@pytest.mark.parametrize(
    ("ticker", "cnpj", "year", "month"),
    [
        ("ALZR11", "28.737.771/0001-85", 2025, 4),
        ("SNAG11", "28.152.777/0001-90", 2023, 8),
    ],
)
def test_confirmed_quota_split_preserves_economic_history(
    ticker: str, cnpj: str, year: int, month: int
) -> None:
    months = [
        (year + (month - 9 + index) // 12, (month - 9 + index) % 12 + 1) for index in range(18)
    ]
    reports = [
        FundMonthlyReport(
            as_of=date(report_year, report_month, 1),
            nav_per_share=Decimal("100") if index < 8 else Decimal("10"),
            issued_shares=Decimal("1000000") if index < 8 else Decimal("10000000"),
            net_assets=Decimal("100000000"),
            monthly_nav_return=Decimal("-0.9") if index == 8 else Decimal("0"),
        )
        for index, (report_year, report_month) in enumerate(months)
    ]
    distributions = [
        FundDistribution(
            ex_date=date(report_year, report_month, 15),
            value=Decimal("1") if index < (9 if ticker == "ALZR11" else 8) else Decimal("0.1"),
            source="cvm",
        )
        for index, (report_year, report_month) in enumerate(months)
    ]

    normalized = normalize_fund_units(ticker, cnpj, reports, distributions)

    assert normalized.reports is not reports
    assert normalized.distributions is not distributions
    assert normalized.reports[0].nav_per_share == Decimal("10")
    assert normalized.reports[0].issued_shares == Decimal("10000000")
    assert normalized.distributions[0].value == Decimal("0.1")
    assert normalized.reports[8].monthly_nav_return == Decimal("0")
    assert _nav_growth(normalized.reports).value == Decimal("0")
    assert _nav_max_drawdown(normalized.reports).value == Decimal("0")
    assert _nav_return_observations(normalized.reports)[8].value == Decimal("0")
    assert _issuance_nav_preservation(normalized.reports).value == Decimal("1")
    periods = _distribution_periods(normalized.distributions, [])
    assert _distribution_growth(periods).value == Decimal("0")
    assert _distribution_cut_frequency(periods).value == Decimal("0")
    assert normalized.sources
    assert not normalized.uncertain_reports
    assert not normalized.uncertain_distributions
    assert reports[0].nav_per_share == Decimal("100")


def test_split_requires_verified_fund_identity() -> None:
    reports = [
        FundMonthlyReport(
            as_of=date(2025, 3, 1),
            nav_per_share=Decimal("100"),
            issued_shares=Decimal("1000000"),
        ),
        FundMonthlyReport(
            as_of=date(2025, 4, 1),
            nav_per_share=Decimal("10"),
            issued_shares=Decimal("10000000"),
        ),
    ]

    normalized = normalize_fund_units("ALZR11", "00000000000000", reports, [])

    assert normalized.reports is reports
    assert not normalized.sources
    assert normalized.uncertain_reports


def test_split_does_not_double_adjust_already_normalized_history() -> None:
    reports = [
        FundMonthlyReport(
            as_of=date(2025, 3, 1), nav_per_share=Decimal("10"), issued_shares=Decimal("10000000")
        ),
        FundMonthlyReport(
            as_of=date(2025, 4, 1), nav_per_share=Decimal("10"), issued_shares=Decimal("10000000")
        ),
    ]

    normalized = normalize_fund_units("ALZR11", "28737771000185", reports, [])

    assert normalized.reports is reports
    assert not normalized.sources


def test_split_with_unverified_report_transition_is_flagged() -> None:
    reports = [
        FundMonthlyReport(as_of=date(2025, 3, 1), nav_per_share=Decimal("100")),
        FundMonthlyReport(as_of=date(2025, 4, 1), nav_per_share=Decimal("10")),
    ]

    normalized = normalize_fund_units("ALZR11", "28737771000185", reports, [])

    assert normalized.reports is reports
    assert normalized.uncertain_reports
    assert "could not be reconciled" in normalized.warnings[0]


def test_distribution_history_can_be_adjusted_when_reports_are_already_comparable() -> None:
    reports = [
        FundMonthlyReport(
            as_of=date(2025, 3, 1), nav_per_share=Decimal("10"), issued_shares=Decimal("10000000")
        ),
        FundMonthlyReport(
            as_of=date(2025, 4, 1), nav_per_share=Decimal("10"), issued_shares=Decimal("10000000")
        ),
    ]
    distributions = [
        FundDistribution(ex_date=date(2025, 4, 15), value=Decimal("1"), source="cvm"),
        FundDistribution(ex_date=date(2025, 5, 15), value=Decimal("0.1"), source="cvm"),
    ]

    normalized = normalize_fund_units("ALZR11", "28737771000185", reports, distributions)

    assert normalized.reports is reports
    assert normalized.distributions[0].value == Decimal("0.1")
    assert len(normalized.sources) == 1


def test_ambiguous_distribution_transition_is_withheld() -> None:
    distributions = [
        FundDistribution(ex_date=date(2025, 3, 15), value=Decimal("1"), source="cvm"),
        FundDistribution(ex_date=date(2025, 4, 15), value=Decimal("0.1"), source="cvm"),
        FundDistribution(ex_date=date(2025, 5, 15), value=Decimal("0.01"), source="cvm"),
    ]

    normalized = normalize_fund_units("ALZR11", "28737771000185", [], distributions)

    assert normalized.uncertain_distributions
    assert normalized.distributions is distributions


def test_net_assets_can_confirm_report_unit_transition_without_share_count() -> None:
    reports = [
        FundMonthlyReport(
            as_of=date(2025, 3, 1),
            nav_per_share=Decimal("100"),
            net_assets=Decimal("100000000"),
        ),
        FundMonthlyReport(
            as_of=date(2025, 4, 1),
            nav_per_share=Decimal("10"),
            net_assets=Decimal("101000000"),
        ),
    ]

    normalized = normalize_fund_units("ALZR11", "28737771000185", reports, [])

    assert normalized.reports[0].nav_per_share == Decimal("10")
    assert not normalized.uncertain_reports


def test_history_on_one_side_of_event_needs_no_adjustment() -> None:
    old_report = FundMonthlyReport(as_of=date(2023, 1, 1), nav_per_share=Decimal("100"))
    new_report = FundMonthlyReport(as_of=date(2026, 1, 1), nav_per_share=Decimal("10"))

    assert normalize_fund_units("ALZR11", "28737771000185", [old_report], []).reports == [
        old_report
    ]
    assert normalize_fund_units("ALZR11", "28737771000185", [new_report], []).reports == [
        new_report
    ]
