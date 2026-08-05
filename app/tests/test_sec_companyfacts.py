from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.cache import CacheStore
from app.config import Settings
from app.models import (
    InstrumentDataResponse,
    InstrumentMetadata,
    InstrumentType,
    InternationalFundamentals,
    MarketQuote,
)
from app.models.quality import QualityAssetKind, QualityAssetRequest, QualityFactsRequest
from app.scrapers import sec_companyfacts as sec_module
from app.scrapers.sec_companyfacts import (
    SecCompanyFactsProvider,
    normalize_cik,
    normalize_sec_ticker,
    parse_company_facts,
)
from app.services.fundamentals import FundamentalsService
from app.services.market import InstrumentDataService
from app.services.opportunity import _instrument_from_b3
from app.services.quality import QualityFactsService


def _observation(
    value: str,
    end: str,
    *,
    start: str | None = None,
    filed: str = "2025-02-01",
    form: str = "10-K",
    accn: str = "0001",
) -> dict[str, object]:
    result: dict[str, object] = {
        "val": value,
        "end": end,
        "filed": filed,
        "form": form,
        "accn": accn,
    }
    if start is not None:
        result["start"] = start
    return result


def _facts_payload() -> dict[str, object]:
    annual_2024: dict[str, str] = {"start": "2024-01-01", "end": "2024-12-31"}
    annual_2023: dict[str, str] = {"start": "2023-01-01", "end": "2023-12-31"}

    def flow(values: list[tuple[str, dict[str, str]]]) -> list[dict[str, object]]:
        return [
            _observation(value, dates["end"], start=dates["start"], accn=accn)
            for accn, (value, dates) in zip(("0001", "0002"), values, strict=True)
        ]

    us_gaap = {
        "RevenueFromContractWithCustomerExcludingAssessedTax": {
            "units": {"USD": flow([("100", annual_2023), ("120", annual_2024)])}
        },
        "GrossProfit": {"units": {"USD": flow([("40", annual_2023), ("50", annual_2024)])}},
        "OperatingIncomeLoss": {"units": {"USD": flow([("20", annual_2023), ("25", annual_2024)])}},
        "NetIncomeLoss": {
            "units": {
                "USD": flow([("10", annual_2023), ("12", annual_2024)])
                + [
                    _observation(
                        "99",
                        "2024-12-31",
                        start="2024-01-01",
                        filed="2025-04-01",
                        form="10-K/A",
                        accn="0000",
                    )
                ]
            }
        },
        "StockholdersEquity": {
            "units": {
                "USD": [
                    _observation("80", "2023-12-31"),
                    _observation("100", "2024-12-31", filed="2025-03-01"),
                ]
            }
        },
        "Assets": {
            "units": {"USD": [_observation("200", "2023-12-31"), _observation("230", "2024-12-31")]}
        },
        "AssetsCurrent": {"units": {"USD": [_observation("90", "2024-12-31")]}},
        "LiabilitiesCurrent": {"units": {"USD": [_observation("30", "2024-12-31")]}},
        "CashAndCashEquivalentsAtCarryingValue": {
            "units": {"USD": [_observation("20", "2024-12-31")]}
        },
        "LongTermDebtCurrent": {"units": {"USD": [_observation("10", "2024-12-31")]}},
        "LongTermDebtNoncurrent": {"units": {"USD": [_observation("40", "2024-12-31")]}},
        "NetCashProvidedByUsedInOperatingActivities": {
            "units": {"USD": flow([("11", annual_2023), ("14", annual_2024)])}
        },
        "PaymentsToAcquirePropertyPlantAndEquipment": {
            "units": {"USD": flow([("3", annual_2023), ("4", annual_2024)])}
        },
        "DepreciationDepletionAndAmortization": {
            "units": {"USD": flow([("5", annual_2023), ("6", annual_2024)])}
        },
        "EarningsPerShareDiluted": {
            "units": {"USD/shares": flow([("1", annual_2023), ("1.2", annual_2024)])}
        },
        "EntityCommonStockSharesOutstanding": {
            "units": {"shares": [_observation("10", "2024-12-31")]}
        },
    }
    return {
        "entityName": "Example SEC Issuer",
        "facts": {"us-gaap": us_gaap},
    }


def test_sec_normalizers_are_strict_and_canonical() -> None:
    assert normalize_sec_ticker(" aapl ") == "AAPL"
    assert normalize_cik("CIK320193") == "0000320193"
    with pytest.raises(ValueError):
        normalize_sec_ticker("AAPL34")
    with pytest.raises(ValueError):
        normalize_cik("12345678901")


def test_companyfacts_maps_annual_history_and_amended_latest_value() -> None:
    result = parse_company_facts(
        _facts_payload(),
        "AAPL",
        "320193",
        "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json",
    )

    assert result is not None
    assert result.company_name == "Example SEC Issuer"
    assert [period.period_end for period in result.years] == [
        date(2023, 12, 31),
        date(2024, 12, 31),
    ]
    assert result.years[-1].net_income == Decimal("99")
    assert result.years[-1].free_cash_flow == Decimal("10")
    assert result.gross_debt == Decimal("50")
    assert result.net_debt == Decimal("30")
    assert result.shares_outstanding == Decimal("10")
    assert result.source == "sec_edgar_companyfacts"
    assert result.source_url and "CIK0000320193" in result.source_url


@pytest.mark.asyncio
async def test_sec_provider_resolves_cik_and_caches_directory_and_facts() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert request.headers["user-agent"].startswith("fundamentus-data-api/")
        if request.url.host == "www.sec.test":
            return httpx.Response(200, json={"0": {"ticker": "AAPL", "cik_str": 320193}})
        return httpx.Response(200, json=_facts_payload())

    settings = Settings(
        sec_edgar_base_url="https://data.sec.test",
        sec_company_tickers_url="https://www.sec.test/files/company_tickers.json",
        retry_attempts=1,
    )
    provider = SecCompanyFactsProvider(settings, httpx.MockTransport(handler))

    first = await provider.statements("AAPL")
    second = await provider.statements("AAPL")

    assert first is not None and second is not None
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_sec_failure_returns_none_for_fallback() -> None:
    provider = SecCompanyFactsProvider(
        Settings(
            sec_edgar_base_url="https://data.sec.test",
            sec_company_tickers_url="https://www.sec.test/files/company_tickers.json",
            retry_attempts=1,
        ),
        httpx.MockTransport(lambda _request: httpx.Response(503)),
    )
    assert await provider.statements("AAPL") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 500])
async def test_sec_http_failures_are_bounded(status: int) -> None:
    responses = iter([httpx.Response(status), httpx.Response(404)])
    provider = SecCompanyFactsProvider(
        Settings(
            sec_edgar_base_url="https://data.sec.test",
            retry_attempts=2,
            retry_backoff_seconds=0,
        ),
        httpx.MockTransport(lambda _request: next(responses)),
    )
    assert await provider._fetch_json("https://data.sec.test", "/facts") is None


@pytest.mark.asyncio
async def test_sec_invalid_json_and_directory_shapes_are_ignored() -> None:
    provider = SecCompanyFactsProvider(
        Settings(sec_request_timeout_seconds=1, retry_attempts=1),
        httpx.MockTransport(lambda request: httpx.Response(200, text="not-json")),
    )
    assert await provider._fetch_json("https://data.sec.test", "/facts") is None
    provider._cache["ticker-directory"] = sec_module._CachedValue(10**20, ["bad"])
    assert await provider.statements("AAPL") is None


@pytest.mark.asyncio
async def test_sec_directory_rejects_bad_rows_and_non_dict_facts() -> None:
    provider = SecCompanyFactsProvider(
        Settings(), httpx.MockTransport(lambda _request: httpx.Response(200, json=[]))
    )
    provider._cache["ticker-directory"] = sec_module._CachedValue(
        10**20,
        [
            {"ticker": 1, "cik_str": 1},
            {"ticker": "AAPL34", "cik_str": 1},
            {"ticker": "NOPE", "cik_str": 1},
            {"ticker": "AAPL", "cik_str": "bad"},
        ],
    )
    assert await provider.statements("AAPL") is None
    provider._cache["ticker-directory"] = sec_module._CachedValue(
        10**20, {"0": {"ticker": "AAPL", "cik_str": 1}}
    )
    provider._cache["facts:0000000001"] = sec_module._CachedValue(10**20, [])
    assert await provider.statements("AAPL") is None


@pytest.mark.asyncio
async def test_sec_retry_handles_transport_exception() -> None:
    responses = iter([httpx.ConnectError("offline"), httpx.Response(200, json={})])

    def handler(_request: httpx.Request) -> httpx.Response:
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        assert isinstance(response, httpx.Response)
        return response

    provider = SecCompanyFactsProvider(
        Settings(retry_attempts=2, retry_backoff_seconds=0),
        httpx.MockTransport(handler),
    )
    assert await provider._fetch_json("https://data.sec.test", "/facts") == {}


def test_sec_parser_helpers_cover_invalid_units_and_dates() -> None:
    assert parse_company_facts({"facts": {}}, "bad ticker", "1", "url") is None
    assert parse_company_facts({"facts": {}}, "AAPL", "bad", "url") is None
    assert sec_module._choose_unit({"USD": "not-list"}, "revenue") == (None, [])
    assert (
        sec_module._parse_tag(
            {"units": {"USD": [None, {"end": "bad"}, {"val": "bad", "end": "2024-12-31"}]}},
            "revenue",
        )
        == []
    )
    assert sec_module._as_date("bad") is None
    assert sec_module._quotient(Decimal("1"), Decimal("0")) is None
    assert sec_module._sum_optional(None, Decimal("2")) == Decimal("2")
    assert sec_module._sum_optional(Decimal("2"), None) == Decimal("2")
    assert sec_module._free_cash_flow(None, Decimal("2")) is None
    assert sec_module._currency({"us-gaap": {}}, sec_module._TAGS) == "USD"


def test_companyfacts_rejects_malformed_payloads_and_supports_ifrs() -> None:
    assert parse_company_facts({}, "AAPL", "1", "url") is None
    assert parse_company_facts({"facts": {}}, "AAPL", "1", "url") is None
    payload = {
        "facts": {
            "ifrs-full": {
                "Revenue": {
                    "units": {
                        "EUR": [
                            {
                                "val": "30",
                                "start": "2024-01-01",
                                "end": "2024-12-31",
                                "form": "20-F",
                                "filed": "2025-03-01",
                            }
                        ]
                    }
                },
                "ProfitLoss": {
                    "units": {
                        "EUR": [
                            {
                                "val": "4",
                                "start": "2024-01-01",
                                "end": "2024-12-31",
                                "form": "20-F",
                                "filed": "2025-03-01",
                            }
                        ]
                    }
                },
            }
        }
    }
    result = parse_company_facts(payload, "NVO", "1", "url")
    assert result is not None
    assert result.currency == "EUR"
    assert result.years[-1].revenue == Decimal("30")


def test_companyfacts_skips_invalid_entries_and_non_annual_periods() -> None:
    payload = {
        "facts": {
            "us-gaap": {
                "Revenue": {
                    "units": {
                        "USD": [
                            {"val": "bad", "end": "2024-12-31"},
                            {
                                "val": "3",
                                "end": "2024-03-31",
                                "start": "2024-01-01",
                                "form": "10-Q",
                            },
                        ]
                    }
                }
            }
        }
    }
    assert parse_company_facts(payload, "AAPL", "1", "url") is None


def test_b3_bdr_metadata_prefers_authoritative_underlying_and_alias_is_safe() -> None:
    payload = {
        "table": {
            "columns": [
                {"name": name}
                for name in (
                    "TckrSymb",
                    "SgmtNm",
                    "SctyCtgyNm",
                    "CrpnNm",
                    "UnderlyingTicker",
                    "UnderlyingISIN",
                )
            ],
            "values": [["TEST34", "CASH", "BDR", "Example BDR", "MSFT", "US5949181045"]],
        }
    }
    instrument = _instrument_from_b3(payload, "TEST34")
    assert instrument is not None
    assert instrument.instrument_type is InstrumentType.bdr
    assert instrument.underlying_ticker == "MSFT"
    assert instrument.underlying_source == "b3"

    alias_payload = {
        "table": {
            "columns": [{"name": name} for name in ("TckrSymb", "SgmtNm", "SctyCtgyNm", "CrpnNm")],
            "values": [["AAPL34", "CASH", "BDR", "Apple BDR"]],
        }
    }
    alias = _instrument_from_b3(alias_payload, "AAPL34")
    assert alias is not None and alias.underlying_ticker == "AAPL"


@pytest.mark.asyncio
async def test_unresolved_bdr_never_queries_international_sources(tmp_path: Path) -> None:
    class FailingSec:
        async def statements(self, _ticker: str) -> None:
            pytest.fail("unresolved BDR must not query SEC")

    class FailingHtml:
        async def statements(self, _ticker: str) -> None:
            pytest.fail("unresolved BDR must not query HTML")

    service = FundamentalsService(
        Settings(),
        CacheStore(sqlite_enabled=False, sqlite_path=tmp_path / "cache.sqlite"),
        international=FailingHtml(),  # type: ignore[arg-type]
        sec=FailingSec(),  # type: ignore[arg-type]
    )
    snapshot = await service.snapshot(
        "TEST34",
        instrument=InstrumentMetadata(
            ticker="TEST34",
            instrument_type=InstrumentType.bdr,
            underlying_unavailable_reason="No authoritative B3 underlying",
        ),
    )
    assert snapshot.unavailable_reason == "No authoritative B3 underlying"


@pytest.mark.asyncio
async def test_instrument_search_is_local_and_bounded() -> None:
    class B3:
        async def get(self, ticker: str) -> InstrumentMetadata:
            return InstrumentMetadata(
                ticker=ticker,
                name="Apple BDR",
                instrument_type=InstrumentType.bdr,
                underlying_ticker="AAPL",
            )

    class Brapi:
        async def get(
            self, _ticker: str
        ) -> tuple[MarketQuote | None, InternationalFundamentals | None]:
            return None, None

    service = InstrumentDataService(
        Settings(instrument_data_ttl_seconds=0),
        b3=B3(),  # type: ignore[arg-type]
        brapi=Brapi(),  # type: ignore[arg-type]
    )
    await service.get("AAPL34")
    result = await service.search("apple", limit=1)
    assert len(result.results) == 1
    assert result.results[0].underlying_ticker == "AAPL"
    assert result.limited


@pytest.mark.asyncio
async def test_unresolved_bdr_quality_does_not_use_local_market_facts() -> None:
    instrument = InstrumentMetadata(
        ticker="UNKNOWN34",
        instrument_type=InstrumentType.bdr,
        isin="BR0000000000",
        underlying_unavailable_reason="No safe underlying alias",
    )
    data = InstrumentDataResponse(
        ticker="UNKNOWN34",
        instrument=instrument,
        fundamentals=InternationalFundamentals(
            market_capitalization=Decimal("100"),
            source="brapi",
        ),
        refreshed_at=datetime.now(UTC),
    )

    class Instruments:
        async def get(self, _ticker: str, _kind: InstrumentType) -> InstrumentDataResponse:
            return data

    class Opportunity:
        async def opportunity(self, _ticker: str) -> SimpleNamespace:
            return SimpleNamespace(instrument=instrument)

    class Fundamentals:
        async def sector_universe(self) -> dict[str, list[object]]:
            return {}

        async def snapshot(self, *_args: object, **_kwargs: object) -> object:
            pytest.fail("unresolved BDR must not ask for fundamentals")

    service = QualityFactsService(
        Fundamentals(),  # type: ignore[arg-type]
        Instruments(),  # type: ignore[arg-type]
        Opportunity(),  # type: ignore[arg-type]
    )
    result = await service.resolve(
        QualityFactsRequest(
            assets=[QualityAssetRequest(ticker="UNKNOWN34", kind=QualityAssetKind.stock)]
        )
    )
    assert result.assets[0].facts == []
    assert result.assets[0].unavailable_reason == "No safe underlying alias"
