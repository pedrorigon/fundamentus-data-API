"""Annual statements for foreign listings must be read exactly as reported."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.scrapers.yahoo_fundamentals import (
    SOURCE_YAHOO,
    YahooFundamentalsProvider,
    parse_yahoo_fundamentals,
)


def _series(series_type: str, values: list[tuple[str, float]]) -> dict[str, Any]:
    return {
        "meta": {"type": [series_type]},
        series_type: [
            {"asOfDate": as_of, "reportedValue": {"raw": value}} for as_of, value in values
        ],
    }


def _payload(*series: dict[str, Any]) -> dict[str, Any]:
    return {"timeseries": {"result": list(series)}}


def test_annual_periods_are_built_from_the_reported_series() -> None:
    payload = _payload(
        _series("annualTotalRevenue", [("2024-12-31", 1000.0), ("2025-12-31", 1200.0)]),
        _series("annualNetIncome", [("2024-12-31", 100.0), ("2025-12-31", 150.0)]),
    )

    snapshot = parse_yahoo_fundamentals("ACME", payload)

    assert snapshot is not None
    assert [period.period_end for period in snapshot.periods] == [
        date(2024, 12, 31),
        date(2025, 12, 31),
    ]
    assert snapshot.periods[-1].revenue == Decimal("1200")
    assert snapshot.periods[-1].net_income == Decimal("150")


def test_periods_are_annual_and_declare_their_source() -> None:
    payload = _payload(_series("annualTotalRevenue", [("2025-12-31", 1000.0)]))

    snapshot = parse_yahoo_fundamentals("ACME", payload)

    assert snapshot is not None
    assert snapshot.periods[0].annual is True
    assert snapshot.periods[0].source == SOURCE_YAHOO


def test_the_latest_period_becomes_the_trailing_reference() -> None:
    payload = _payload(
        _series("annualTotalRevenue", [("2024-12-31", 1000.0), ("2025-12-31", 1200.0)]),
    )

    snapshot = parse_yahoo_fundamentals("ACME", payload)

    assert snapshot is not None
    assert snapshot.trailing_twelve_months is not None
    assert snapshot.trailing_twelve_months.period_end == date(2025, 12, 31)


def test_per_share_figures_are_derived_from_the_share_count() -> None:
    payload = _payload(
        _series("annualNetIncome", [("2025-12-31", 1000.0)]),
        _series("annualStockholdersEquity", [("2025-12-31", 5000.0)]),
        _series("annualOrdinarySharesNumber", [("2025-12-31", 100.0)]),
    )

    snapshot = parse_yahoo_fundamentals("ACME", payload)

    assert snapshot is not None
    assert snapshot.earnings_per_share == Decimal("10")
    assert snapshot.book_value_per_share == Decimal("50")


def test_per_share_figures_need_a_positive_share_count() -> None:
    payload = _payload(
        _series("annualNetIncome", [("2025-12-31", 1000.0)]),
        _series("annualOrdinarySharesNumber", [("2025-12-31", 0.0)]),
    )

    snapshot = parse_yahoo_fundamentals("ACME", payload)

    assert snapshot is not None
    assert snapshot.earnings_per_share is None


def test_interest_expense_is_stored_as_a_signed_financial_result() -> None:
    """The source reports a positive cost where this model expects a signed effect."""
    payload = _payload(_series("annualInterestExpense", [("2025-12-31", 250.0)]))

    snapshot = parse_yahoo_fundamentals("ACME", payload)

    assert snapshot is not None
    assert snapshot.periods[0].financial_result == Decimal("-250")


def test_capital_expenditure_is_stored_as_the_invested_amount() -> None:
    payload = _payload(_series("annualCapitalExpenditure", [("2025-12-31", -900.0)]))

    snapshot = parse_yahoo_fundamentals("ACME", payload)

    assert snapshot is not None
    assert snapshot.periods[0].capex == Decimal("900")


def test_unreported_lines_stay_absent() -> None:
    """An absent line must not be read as a zero."""
    payload = _payload(_series("annualTotalRevenue", [("2025-12-31", 1000.0)]))

    snapshot = parse_yahoo_fundamentals("ACME", payload)

    assert snapshot is not None
    assert snapshot.periods[0].ebitda is None


@pytest.mark.parametrize(
    "payload",
    [{}, {"timeseries": {}}, {"timeseries": {"result": []}}, {"timeseries": {"result": "x"}}],
)
def test_a_payload_without_results_produces_no_snapshot(payload: dict[str, Any]) -> None:
    assert parse_yahoo_fundamentals("ACME", payload) is None


def test_observations_without_a_date_or_value_are_skipped() -> None:
    payload = _payload(
        {
            "meta": {"type": ["annualTotalRevenue"]},
            "annualTotalRevenue": [
                {"asOfDate": "not-a-date", "reportedValue": {"raw": 1.0}},
                {"asOfDate": "2025-12-31", "reportedValue": {}},
                "unexpected",
            ],
        }
    )

    assert parse_yahoo_fundamentals("ACME", payload) is None


def test_unknown_series_are_ignored() -> None:
    payload = _payload(
        _series("annualSomethingElse", [("2025-12-31", 5.0)]),
        _series("annualTotalRevenue", [("2025-12-31", 1000.0)]),
    )

    snapshot = parse_yahoo_fundamentals("ACME", payload)

    assert snapshot is not None
    assert snapshot.periods[0].revenue == Decimal("1000")


async def test_provider_reads_the_annual_timeseries() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json=_payload(_series("annualTotalRevenue", [("2025-12-31", 1000.0)])),
        )

    provider = YahooFundamentalsProvider(
        Settings(),
        transport=httpx.MockTransport(handler),
    )

    snapshot = await provider.snapshot("AAPL")

    assert snapshot is not None
    assert snapshot.periods[0].revenue == Decimal("1000")
    assert "AAPL" in captured["url"]


@pytest.mark.parametrize("status", [401, 403, 404])
async def test_provider_reports_nothing_for_an_unknown_listing(status: int) -> None:
    provider = YahooFundamentalsProvider(
        Settings(),
        transport=httpx.MockTransport(lambda _request: httpx.Response(status)),
    )

    assert await provider.snapshot("NOPE") is None


async def test_provider_raises_for_an_upstream_failure() -> None:
    """A server fault is not evidence that the company has no statements."""
    provider = YahooFundamentalsProvider(
        Settings(),
        transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
    )

    with pytest.raises(httpx.HTTPStatusError):
        await provider.snapshot("AAPL")


async def test_provider_ignores_a_payload_that_is_not_an_object() -> None:
    provider = YahooFundamentalsProvider(
        Settings(),
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=[1, 2])),
    )

    assert await provider.snapshot("AAPL") is None
