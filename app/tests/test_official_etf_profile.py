from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx
import pytest

from app.config import Settings
from app.models import InstrumentMetadata, InstrumentType
from app.scrapers.official_etf_profile import OfficialEtfProfileProvider


def _instrument(**changes: str) -> InstrumentMetadata:
    return InstrumentMetadata(
        ticker=changes.get("ticker", "ABTC11"),
        instrument_type=InstrumentType.etf,
        isin=changes.get("isin", "BRABTCCTF002"),
        source=changes.get("source", "b3"),
        confidence=changes.get("confidence", "high"),
    )


def _manager_transport(
    *,
    ticker: str = "ABTC11",
    isin: str = "BRABTCCTF002",
    fee: str = "0.39% a.a.",
    observed: str = "23/09/2026T00:00:00Z",
    assets: object = 23_141_900.56,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["ID"] == "126"
        if request.url.path.endswith("/Caracteristica/"):
            return httpx.Response(
                200,
                json={
                    "ErrorWarn": False,
                    "ObjJsonResultado": {
                        "Ticker": ticker,
                        "CodISINFundo": isin,
                        "TaxaAdministracao": fee,
                        "DataInicio": "14/07/2026T00:00:00Z",
                        "IndiceReferencia": "TEVA BITCOIN FEAR ARBITRAGE",
                    },
                },
            )
        assert request.url.path.endswith("/Rentabilidade/GetEvolucaoDiaria/")
        assert request.url.params["ano"] == "2026"
        assert request.url.params["mes"] in {"9", "10"}
        return httpx.Response(
            200,
            json={
                "ErrorWarn": False,
                "ObjJsonResultado": {
                    "EvolucaoDiaria": [
                        {"Data": "22/09/2026T00:00:00Z", "PatrimonioLiquido": 23_318_818.00},
                        {"Data": observed, "PatrimonioLiquido": assets},
                    ]
                },
            },
        )

    return httpx.MockTransport(handler)


async def test_official_etf_profile_uses_current_fee_and_dated_assets() -> None:
    provider = OfficialEtfProfileProvider(Settings(), _manager_transport())

    profile = await provider.get(_instrument(), today=date(2026, 9, 24))

    assert profile is not None
    assert profile.net_expense_ratio == Decimal("0.0039")
    assert profile.net_assets == Decimal("23141900.56")
    assert profile.net_assets_date == date(2026, 9, 23)
    assert profile.inception_date == date(2026, 7, 14)
    assert profile.description == "TEVA BITCOIN FEAR ARBITRAGE"
    assert profile.net_assets_source == profile.source
    assert profile.source == "https://www.btgpactual.com/asset-management/etf/ABTC11"


@pytest.mark.parametrize(
    "changes",
    [
        {"ticker": "OTHER11"},
        {"isin": "BROTHERCTF00"},
        {"source": "secondary"},
        {"confidence": "low"},
    ],
)
async def test_official_etf_profile_requires_verified_exchange_identity(
    changes: dict[str, str],
) -> None:
    provider = OfficialEtfProfileProvider(
        Settings(), httpx.MockTransport(lambda _request: pytest.fail("network request"))
    )

    assert await provider.get(_instrument(**changes), today=date(2026, 9, 24)) is None


@pytest.mark.parametrize(
    ("ticker", "isin"),
    [("OTHER11", "BRABTCCTF002"), ("ABTC11", "BROTHERCTF00")],
)
async def test_manager_payload_must_repeat_listing_identity(ticker: str, isin: str) -> None:
    provider = OfficialEtfProfileProvider(Settings(), _manager_transport(ticker=ticker, isin=isin))

    assert await provider.get(_instrument(), today=date(2026, 9, 24)) is None


async def test_stale_or_future_assets_are_withheld_without_inventing_a_current_value() -> None:
    provider = OfficialEtfProfileProvider(
        Settings(), _manager_transport(observed="25/09/2026T00:00:00Z")
    )
    future = await provider.get(_instrument(), today=date(2026, 9, 24))
    assert future is not None
    assert future.net_assets == Decimal("23318818.0")
    assert future.net_assets_date == date(2026, 9, 22)

    stale = await provider.get(_instrument(), today=date(2026, 10, 5))
    assert stale is not None
    assert stale.net_expense_ratio == Decimal("0.0039")
    assert stale.net_assets is None
    assert stale.net_assets_date is None


async def test_previous_month_assets_are_used_only_inside_the_freshness_window() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/Caracteristica/"):
            return httpx.Response(
                200,
                json={
                    "ErrorWarn": False,
                    "ObjJsonResultado": {
                        "Ticker": "ABTC11",
                        "CodISINFundo": "BRABTCCTF002",
                        "TaxaAdministracao": "0.39% a.a.",
                    },
                },
            )
        rows = (
            [{"Data": "30/09/2026T00:00:00Z", "PatrimonioLiquido": 24_000_000}]
            if request.url.params["mes"] == "9"
            else []
        )
        return httpx.Response(
            200, json={"ErrorWarn": False, "ObjJsonResultado": {"EvolucaoDiaria": rows}}
        )

    provider = OfficialEtfProfileProvider(Settings(), httpx.MockTransport(handler))

    profile = await provider.get(_instrument(), today=date(2026, 10, 2))

    assert profile is not None
    assert profile.net_assets == Decimal("24000000")
    assert profile.net_assets_date == date(2026, 9, 30)


async def test_invalid_fee_and_assets_do_not_create_a_profile() -> None:
    provider = OfficialEtfProfileProvider(
        Settings(), _manager_transport(fee="0.39", assets="not-a-number")
    )

    # The independent prior-day observation remains usable, but it cannot
    # make an unparseable fee look like a documented zero-cost fund.
    profile = await provider.get(_instrument(), today=date(2026, 9, 24))
    assert profile is not None
    assert profile.net_expense_ratio is None
    assert profile.net_assets == Decimal("23318818.0")

    no_current_value = await provider.get(_instrument(), today=date(2026, 10, 5))
    assert no_current_value is None


@pytest.mark.parametrize("assets", ["NaN", "Infinity", "-1"])
async def test_invalid_asset_values_cannot_be_used(assets: str) -> None:
    provider = OfficialEtfProfileProvider(
        Settings(), _manager_transport(observed="23/09/2026T00:00:00Z", assets=assets)
    )

    profile = await provider.get(_instrument(), today=date(2026, 9, 24))

    assert profile is not None
    assert profile.net_assets == Decimal("23318818.0")
    assert profile.net_assets_date == date(2026, 9, 22)


async def test_manager_failure_is_an_explicitly_unavailable_profile() -> None:
    provider = OfficialEtfProfileProvider(
        Settings(), httpx.MockTransport(lambda _request: httpx.Response(503))
    )

    assert await provider.get(_instrument(), today=date(2026, 9, 24)) is None
