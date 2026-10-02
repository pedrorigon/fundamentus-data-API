from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import httpx
import pytest
from openpyxl import Workbook

from app.config import Settings
from app.models import InstrumentMetadata, InstrumentType
from app.scrapers.official_etf_profile import (
    OfficialEtfProfileProvider,
    _finite_decimal,
    _parse_holdings,
)

_XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


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
    holdings: bytes | None = None,
    holdings_content_type: str = _XLSX_CONTENT_TYPE,
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
        if request.url.path.endswith("/Composicao/DownloadCarteira/"):
            return (
                httpx.Response(
                    200,
                    content=holdings,
                    headers={"content-type": holdings_content_type},
                )
                if holdings is not None
                else httpx.Response(404)
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


def _manager_holdings(*, observed: datetime = datetime(2026, 9, 23)) -> bytes:
    return _workbook_content(
        [
            [observed, "Outros", "XBT", 1, 900, 900, 90.09],
            [observed, "Renda fixa", "LFT REF", 1, 100, 100, 10.01],
            [observed, "Outros", "DESPESA AUDITORIA", 1, -1, -1, -0.1],
        ]
    )


def _workbook_content(
    rows: list[list[object]],
    *,
    sheet_name: str = "Carteira",
    leading_rows: int = 0,
    include_header: bool = True,
) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = sheet_name
    for _ in range(leading_rows):
        sheet.append(["intro"])
    if include_header:
        sheet.append(
            [
                "DATA",
                "CATEGORIA",
                "ATIVO",
                "QUANTIDADE",
                "PREÇO (R$)",
                "FINANCEIRO (R$)",
                "PESO (%)",
            ]
        )
    for row in rows:
        sheet.append(row)
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


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


async def test_official_etf_profile_resolves_dated_manager_holdings() -> None:
    provider = OfficialEtfProfileProvider(
        Settings(), _manager_transport(holdings=_manager_holdings())
    )

    profile = await provider.get(_instrument(), today=date(2026, 9, 24))

    assert profile is not None
    assert [(item.symbol, item.weight) for item in profile.holdings] == [
        ("XBT", Decimal("0.9")),
        ("LFT REF", Decimal("0.1")),
    ]
    assert profile.holdings_date == date(2026, 9, 23)
    assert profile.holdings_grouped_by_label is True
    assert profile.holdings_source == (
        "https://www.btgpactual.com/etf/api/Composicao/DownloadCarteira/?ID=126"
    )


@pytest.mark.parametrize("observed", [datetime(2026, 9, 25), datetime(2026, 9, 15)])
async def test_future_or_stale_holdings_do_not_replace_valid_fees(
    observed: datetime,
) -> None:
    provider = OfficialEtfProfileProvider(
        Settings(), _manager_transport(holdings=_manager_holdings(observed=observed))
    )

    profile = await provider.get(_instrument(), today=date(2026, 9, 24))

    assert profile is not None
    assert profile.net_expense_ratio == Decimal("0.0039")
    assert profile.holdings == []
    assert profile.holdings_date is None


async def test_invalid_manager_workbook_is_ignored_without_inventing_holdings() -> None:
    provider = OfficialEtfProfileProvider(Settings(), _manager_transport(holdings=b"not-xlsx"))

    profile = await provider.get(_instrument(), today=date(2026, 9, 24))

    assert profile is not None
    assert profile.holdings == []


async def test_manager_workbook_requires_spreadsheet_content_type() -> None:
    provider = OfficialEtfProfileProvider(
        Settings(),
        _manager_transport(holdings=_manager_holdings(), holdings_content_type="text/html"),
    )

    profile = await provider.get(_instrument(), today=date(2026, 9, 24))

    assert profile is not None
    assert profile.holdings == []


def test_manager_workbook_rejects_unknown_sheet_and_missing_header() -> None:
    rows = [[datetime(2026, 9, 23), "Outros", "XBT", 1, 900, 900, 100]]
    assert _parse_holdings(_workbook_content(rows, sheet_name="Other"), date(2026, 9, 24)) == (
        [],
        None,
    )
    assert _parse_holdings(_workbook_content(rows, include_header=False), date(2026, 9, 24)) == (
        [],
        None,
    )
    assert _parse_holdings(_workbook_content(rows, leading_rows=20), date(2026, 9, 24)) == (
        [],
        None,
    )


def test_manager_workbook_rejects_mixed_dates_and_incomplete_total() -> None:
    first = [datetime(2026, 9, 23), "Outros", "XBT", 1, 900, 900, 90]
    second = [datetime(2026, 9, 22), "Renda fixa", "LFT", 1, 100, 100, 10]
    assert _parse_holdings(_workbook_content([first, second]), date(2026, 9, 24)) == (
        [],
        None,
    )
    second[0] = first[0]
    second[6] = 0
    assert _parse_holdings(_workbook_content([first, second]), date(2026, 9, 24)) == (
        [],
        None,
    )


def test_manager_workbook_rejects_malformed_rows_and_large_archive() -> None:
    as_of = datetime(2026, 9, 23)
    malformed = [as_of, "Outros", "", 1, 900, 900, 100]
    assert _parse_holdings(_workbook_content([malformed]), date(2026, 9, 24)) == (
        [],
        None,
    )
    malformed[2] = "XBT"
    malformed[0] = "23/09/2026"
    assert _parse_holdings(_workbook_content([malformed]), date(2026, 9, 24)) == (
        [],
        None,
    )
    archive = BytesIO()
    with ZipFile(archive, "w", compression=ZIP_DEFLATED) as output:
        for index in range(41):
            output.writestr(f"file-{index}.xml", "x")
    assert _parse_holdings(archive.getvalue(), date(2026, 9, 24)) == ([], None)


def test_manager_workbook_skips_empty_rows_without_changing_total_weights() -> None:
    as_of = datetime(2026, 9, 23)
    content = _workbook_content(
        [
            [as_of, "Outros", "XBT", 1, 60, 60, 60],
            [None] * 7,
            [as_of, "Renda fixa", "LFT", 1, 40, 40, 40],
        ]
    )

    holdings, holdings_date = _parse_holdings(content, date(2026, 9, 24))

    assert holdings_date == date(2026, 9, 23)
    assert [(item.symbol, item.weight) for item in holdings] == [
        ("XBT", Decimal("0.6")),
        ("LFT", Decimal("0.4")),
    ]


def test_manager_workbook_rejects_more_than_500_portfolio_rows() -> None:
    as_of = datetime(2026, 9, 23)
    rows = [[as_of, "Outros", "XBT", 1, 1, 1, 1] for _ in range(501)]

    assert _parse_holdings(_workbook_content(rows), date(2026, 9, 24)) == ([], None)


@pytest.mark.parametrize("value", [True, "not-a-number", Decimal("NaN")])
def test_manager_workbook_rejects_invalid_numeric_values(value: object) -> None:
    assert _finite_decimal(value) is None


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
        if request.url.path.endswith("/Composicao/DownloadCarteira/"):
            return httpx.Response(404)
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
