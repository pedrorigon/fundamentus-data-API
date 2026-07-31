from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.cache import CacheStore
from app.config import Settings
from app.core.errors import InvalidTickerError
from app.models.fundamentals import FinancialPeriod, FundamentalsSnapshot
from app.parsers.cvm_statements import (
    ACCOUNT_EBIT,
    ACCOUNT_EQUITY,
    ACCOUNT_NET_INCOME,
    ACCOUNT_OPERATING_CASH_FLOW,
    ACCOUNT_REVENUE,
    CompanyRegistration,
    ShareCapital,
    StatementPeriod,
)
from app.scrapers.cvm_open_data import CvmOpenDataProvider, StatementArchive, StatementKind
from app.scrapers.international_listings import InternationalListing
from app.services.fundamentals import FundamentalsService

CNPJ = "11111111000111"
COMPANY = "EMPRESA TESTE S.A."


def settings(tmp_path: Path) -> Settings:
    return Settings(
        sqlite_cache_enabled=False,
        sqlite_cache_path=tmp_path / "cache.sqlite3",
        fundamentals_history_years=2,
    )


def period(
    end: date,
    *,
    start: date | None = None,
    revenue: str = "1000",
) -> StatementPeriod:
    return StatementPeriod(
        cnpj=CNPJ,
        cvm_code="001",
        company_name=COMPANY,
        reference_date=end,
        period_start=start or date(end.year, 1, 1),
        period_end=end,
        consolidated=True,
        accounts={
            ACCOUNT_REVENUE: Decimal(revenue),
            ACCOUNT_EBIT: Decimal("250"),
            ACCOUNT_NET_INCOME: Decimal("150"),
            ACCOUNT_EQUITY: Decimal("900"),
            ACCOUNT_OPERATING_CASH_FLOW: Decimal("300"),
        },
        depreciation=Decimal("50"),
    )


class StubProvider(CvmOpenDataProvider):
    def __init__(
        self,
        archives: dict[tuple[str, int], StatementArchive | None] | None = None,
        registry: dict[str, CompanyRegistration] | None = None,
    ) -> None:
        self.archives = archives or {}
        self.registry_data = registry or {}
        self.statement_calls: list[tuple[str, int]] = []
        self.registry_calls = 0

    async def statements(
        self,
        kind: StatementKind,
        year: int,
        cnpjs: set[str] | None = None,
    ) -> StatementArchive | None:
        self.statement_calls.append((kind.value, year))
        return self.archives.get((kind.value, year))

    async def registry(self) -> dict[str, CompanyRegistration]:
        self.registry_calls += 1
        return self.registry_data


def archive(year: int, periods: list[StatementPeriod], shares: str = "1000") -> StatementArchive:
    return StatementArchive(
        kind=StatementKind.ANNUAL,
        year=year,
        periods={CNPJ: periods},
        share_capital={
            CNPJ: ShareCapital(
                cnpj=CNPJ,
                reference_date=date(year, 12, 31),
                common_shares=Decimal(shares),
                preferred_shares=Decimal("0"),
                treasury_shares=Decimal("0"),
            )
        },
    )


class StubInternationalProvider:
    """Stands in for the public indicator pages, which tests never reach."""

    def __init__(self, listing: InternationalListing | None = None) -> None:
        self.listing_value = listing
        self.calls: list[str] = []

    async def listing(self, ticker: str) -> InternationalListing | None:
        self.calls.append(ticker)
        return self.listing_value


async def build(
    tmp_path: Path,
    provider: CvmOpenDataProvider,
    international: StubInternationalProvider | None = None,
) -> FundamentalsService:
    config = settings(tmp_path)
    cache = CacheStore(sqlite_enabled=False, sqlite_path=config.sqlite_cache_path)
    await cache.startup()
    return FundamentalsService(
        config,
        cache,
        provider,
        international or StubInternationalProvider(),  # type: ignore[arg-type]
    )


async def test_resolves_snapshot_from_primary_source(tmp_path: Path) -> None:
    provider = StubProvider(
        {("dfp", 2024): archive(2024, [period(date(2024, 12, 31))])},
        registry={
            CNPJ: CompanyRegistration(
                cnpj=CNPJ,
                cvm_code="001",
                corporate_name=COMPANY,
                trade_name=None,
                sector="Comércio",
                status="ATIVO",
            )
        },
    )
    service = await build(tmp_path, provider)

    snapshot = await service.snapshot("TEST4", COMPANY, reference=date(2024, 12, 31))

    assert snapshot.cnpj == CNPJ
    assert snapshot.sector == "Comércio"
    assert snapshot.shares_outstanding == Decimal("1000")
    assert snapshot.trailing_twelve_months is not None
    assert snapshot.trailing_twelve_months.ebitda == Decimal("300")
    assert snapshot.unavailable_reason is None
    assert ("itr", 2024) in provider.statement_calls
    assert ("itr", 2023) in provider.statement_calls


async def test_reconciles_cvm_share_units_with_market_reference(tmp_path: Path) -> None:
    provider = StubProvider(
        {("dfp", 2024): archive(2024, [period(date(2024, 12, 31))], shares="11021")}
    )
    service = await build(tmp_path, provider)

    snapshot = await service.snapshot(
        "TEST4",
        COMPANY,
        reference=date(2024, 12, 31),
        reference_shares=Decimal("11026000"),
    )

    assert snapshot.shares_outstanding == Decimal("11026000")
    assert snapshot.periods[0].shares_outstanding == Decimal("11021000")
    shares_provenance = next(
        item for item in snapshot.provenance if item.field_name == "shares_outstanding"
    )
    assert shares_provenance.selected_source == "fundamentus"


async def test_reconciles_share_units_from_independent_per_share_values(
    tmp_path: Path,
) -> None:
    provider = StubProvider(
        {("dfp", 2024): archive(2024, [period(date(2024, 12, 31))], shares="11")}
    )
    service = await build(tmp_path, provider)

    snapshot = await service.snapshot(
        "TEST4",
        COMPANY,
        reference=date(2024, 12, 31),
        earnings_per_share=Decimal("0.013636363636363636"),
        book_value_per_share=Decimal("0.081818181818181818"),
    )

    assert snapshot.shares_outstanding is not None
    assert abs(snapshot.shares_outstanding - Decimal("11000")) < Decimal("0.001")
    assert snapshot.periods[0].shares_outstanding == Decimal("11000")
    shares_provenance = next(
        item for item in snapshot.provenance if item.field_name == "shares_outstanding"
    )
    assert shares_provenance.selected_source == "derived_public_indicators"


async def test_reports_reason_when_every_archive_is_unavailable(tmp_path: Path) -> None:
    service = await build(tmp_path, StubProvider())

    snapshot = await service.snapshot("TEST4", COMPANY, reference=date(2024, 12, 31))

    assert snapshot.trailing_twelve_months is None
    assert snapshot.unavailable_reason == "CVM statement archives are unavailable"


async def test_falls_back_to_older_archive_when_latest_year_missing(tmp_path: Path) -> None:
    provider = StubProvider({("dfp", 2023): archive(2023, [period(date(2023, 12, 31))])})
    service = await build(tmp_path, provider)

    snapshot = await service.snapshot("TEST4", COMPANY, reference=date(2024, 12, 31))

    assert snapshot.cnpj == CNPJ
    assert snapshot.periods[-1].period_end == date(2023, 12, 31)


async def test_reports_reason_when_company_does_not_match(tmp_path: Path) -> None:
    provider = StubProvider({("dfp", 2024): archive(2024, [period(date(2024, 12, 31))])})
    service = await build(tmp_path, provider)

    snapshot = await service.snapshot("TEST4", "OUTRA COMPANHIA XYZ", reference=date(2024, 12, 31))

    assert snapshot.unavailable_reason == "No CVM filing matched this company"


async def test_requires_corporate_name(tmp_path: Path) -> None:
    service = await build(tmp_path, StubProvider())

    snapshot = await service.snapshot("TEST4", None)

    assert snapshot.unavailable_reason == "Corporate name is required to resolve CVM filings"


async def test_rejects_invalid_ticker(tmp_path: Path) -> None:
    service = await build(tmp_path, StubProvider())

    with pytest.raises(InvalidTickerError):
        await service.snapshot("!!", COMPANY)


async def test_second_call_is_served_from_cache(tmp_path: Path) -> None:
    provider = StubProvider({("dfp", 2024): archive(2024, [period(date(2024, 12, 31))])})
    service = await build(tmp_path, provider)

    await service.snapshot("TEST4", COMPANY, reference=date(2024, 12, 31))
    calls_after_first = len(provider.statement_calls)
    snapshot = await service.snapshot("TEST3", COMPANY, reference=date(2024, 12, 31))

    assert len(provider.statement_calls) == calls_after_first
    assert snapshot.cnpj == CNPJ


async def test_registry_is_fetched_once_across_calls(tmp_path: Path) -> None:
    provider = StubProvider(
        {("dfp", 2024): archive(2024, [period(date(2024, 12, 31))])},
        registry={
            CNPJ: CompanyRegistration(
                cnpj=CNPJ,
                cvm_code="001",
                corporate_name=COMPANY,
                trade_name=None,
                sector="Comércio",
                status="ATIVO",
            )
        },
    )
    service = await build(tmp_path, provider)

    await service.snapshot("TEST4", COMPANY, reference=date(2024, 12, 31))
    await service.snapshot("TEST3", COMPANY, reference=date(2024, 12, 31))

    assert provider.registry_calls == 1


async def test_sector_universe_compares_trailing_twelve_month_periods(tmp_path: Path) -> None:
    annual = archive(2024, [period(date(2024, 12, 31), revenue="1000")])
    prior_interim = StatementArchive(
        kind=StatementKind.QUARTERLY,
        year=2024,
        periods={
            CNPJ: [
                period(
                    date(2024, 3, 31),
                    start=date(2024, 1, 1),
                    revenue="200",
                )
            ]
        },
        share_capital={},
    )
    current_interim = StatementArchive(
        kind=StatementKind.QUARTERLY,
        year=2025,
        periods={
            CNPJ: [
                period(
                    date(2025, 3, 31),
                    start=date(2025, 1, 1),
                    revenue="300",
                )
            ]
        },
        share_capital={},
    )
    provider = StubProvider(
        {
            ("dfp", 2024): annual,
            ("itr", 2024): prior_interim,
            ("itr", 2025): current_interim,
        },
        registry={
            CNPJ: CompanyRegistration(
                cnpj=CNPJ,
                cvm_code="001",
                corporate_name=COMPANY,
                trade_name=None,
                sector="Comércio",
                status="ATIVO",
            )
        },
    )
    service = await build(tmp_path, provider)

    universe = await service.sector_universe(reference=date(2025, 4, 30))

    assert universe["Comércio"][0].period.period_end == date(2025, 3, 31)
    assert universe["Comércio"][0].period.revenue == Decimal("1100")


async def test_missing_sector_is_reported_as_none(tmp_path: Path) -> None:
    provider = StubProvider({("dfp", 2024): archive(2024, [period(date(2024, 12, 31))])})
    service = await build(tmp_path, provider)

    snapshot = await service.snapshot("TEST4", COMPANY, reference=date(2024, 12, 31))

    assert snapshot.sector is None


async def test_provenance_marks_available_and_missing_fields(tmp_path: Path) -> None:
    provider = StubProvider({("dfp", 2024): archive(2024, [period(date(2024, 12, 31))])})
    service = await build(tmp_path, provider)

    snapshot = await service.snapshot("TEST4", COMPANY, reference=date(2024, 12, 31))

    statuses = {item.field_name: item.status for item in snapshot.provenance}
    assert statuses["net_income_ttm"] == "valid"
    # Net debt has no debt accounts in the fixture, so it must be reported missing
    # rather than silently defaulting to zero.
    assert statuses["net_debt"] == "missing_data"
    sources = {item.selected_source for item in snapshot.provenance if item.value is not None}
    assert sources == {"cvm"}


async def test_peer_group_requires_minimum_sample(tmp_path: Path) -> None:
    service = await build(tmp_path, StubProvider())

    group = await service.peer_group("Comércio", [])

    assert group.sample_size == 0
    assert group.unavailable_reason == "Not enough peers with comparable fundamentals"


def snapshot_with(net_income: str, equity: str, ticker: str) -> FundamentalsSnapshot:
    return FundamentalsSnapshot(
        ticker=ticker,
        shares_outstanding=Decimal("100"),
        trailing_twelve_months=FinancialPeriod(
            period_end=date(2024, 12, 31),
            consolidated=True,
            annual=True,
            net_income=Decimal(net_income),
            equity=Decimal(equity),
            ebitda=Decimal("400"),
            ebit=Decimal("300"),
            free_cash_flow=Decimal("200"),
            net_debt=Decimal("0"),
        ),
    )


async def test_peer_group_computes_medians(tmp_path: Path) -> None:
    service = await build(tmp_path, StubProvider())
    snapshots = [
        (snapshot_with("100", "1000", "AAAA3"), Decimal("10")),
        (snapshot_with("200", "1000", "BBBB3"), Decimal("10")),
        (snapshot_with("400", "1000", "CCCC3"), Decimal("10")),
    ]

    group = await service.peer_group("Comércio", snapshots)

    assert group.sample_size == 3
    # Market cap 1000 against net income 100/200/400 gives P/E 10/5/2.5.
    assert group.median_price_to_earnings == Decimal("5")
    assert group.median_price_to_book == Decimal("1")


async def test_peer_group_skips_assets_without_usable_data(tmp_path: Path) -> None:
    service = await build(tmp_path, StubProvider())
    incomplete = FundamentalsSnapshot(ticker="DDDD3", shares_outstanding=Decimal("100"))
    snapshots = [
        (snapshot_with("100", "1000", "AAAA3"), Decimal("10")),
        (snapshot_with("200", "1000", "BBBB3"), Decimal("10")),
        (incomplete, Decimal("10")),
        (snapshot_with("400", "1000", "CCCC3"), Decimal("0")),
    ]

    group = await service.peer_group("Comércio", snapshots)

    assert group.tickers == ["AAAA3", "BBBB3"]
    assert group.unavailable_reason is not None


async def test_provider_returns_none_on_transport_error(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    provider = CvmOpenDataProvider(settings(tmp_path), transport=httpx.MockTransport(handler))

    assert await provider.statements(StatementKind.ANNUAL, 2024) is None
    assert await provider.registry() == {}


async def test_provider_returns_none_on_not_found(tmp_path: Path) -> None:
    provider = CvmOpenDataProvider(
        settings(tmp_path),
        transport=httpx.MockTransport(lambda request: httpx.Response(404)),
    )

    assert await provider.statements(StatementKind.ANNUAL, 1990) is None


async def test_provider_returns_none_on_server_error(tmp_path: Path) -> None:
    provider = CvmOpenDataProvider(
        settings(tmp_path),
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )

    assert await provider.statements(StatementKind.ANNUAL, 2024) is None


async def test_provider_parses_downloaded_archive(tmp_path: Path) -> None:
    from app.tests.test_cvm_statements import CNPJ_DIGITS, build_archive, statement_row

    payload = build_archive([statement_row(ACCOUNT_REVENUE, "1000.0000000000")])
    provider = CvmOpenDataProvider(
        settings(tmp_path),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=payload)),
    )

    result = await provider.statements(StatementKind.ANNUAL, 2024)

    assert result is not None
    assert result.year == 2024
    assert CNPJ_DIGITS in result.periods


async def test_provider_parses_downloaded_registry(tmp_path: Path) -> None:
    payload = (
        "CNPJ_CIA;DENOM_SOCIAL;DENOM_COMERC;CD_CVM;SETOR_ATIV;SIT\n"
        "11.111.111/0001-11;EMPRESA TESTE S.A.;TESTE;001;Comércio;ATIVO\n"
    ).encode("latin-1")
    provider = CvmOpenDataProvider(
        settings(tmp_path),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=payload)),
    )

    registry = await provider.registry()

    assert registry[CNPJ].sector == "Comércio"


async def test_endpoint_returns_snapshot(tmp_path: Path) -> None:
    from app.api.dependencies import get_fundamentals_service, get_opportunity_service
    from app.main import create_app
    from app.models import InstrumentMetadata, InstrumentType

    # The endpoint resolves against the current year, so the fixture follows it
    # instead of a fixed year that would age out.
    year = datetime.now(UTC).date().year
    provider = StubProvider({("dfp", year): archive(year, [period(date(year, 12, 31))])})
    service = await build(tmp_path, provider)

    class StubOpportunity:
        async def opportunity(self, ticker: str) -> SimpleNamespace:
            instrument = InstrumentMetadata(
                ticker=ticker,
                name=COMPANY,
                instrument_type=InstrumentType.stock,
                category=None,
                cfi_code=None,
                isin=None,
                currency="BRL",
                reference_date=None,
            )

            def metric(value: Decimal) -> SimpleNamespace:
                return SimpleNamespace(value=value, sources=["fundamentus"])

            return SimpleNamespace(
                instrument=instrument,
                metrics=SimpleNamespace(
                    shares_outstanding=metric(Decimal("1000")),
                    earnings_per_share=metric(Decimal("4")),
                    book_value_per_share=metric(Decimal("9")),
                    dividends_12m=metric(Decimal("2")),
                ),
            )

    app = create_app()
    app.dependency_overrides[get_fundamentals_service] = lambda: service
    app.dependency_overrides[get_opportunity_service] = StubOpportunity
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/v1/assets/TEST4/fundamentals")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ticker"] == "TEST4"
    assert payload["snapshot"]["cnpj"] == CNPJ
    assert payload["snapshot"]["trailing_twelve_months"]["ebitda"] == "300"
    assert payload["snapshot"]["earnings_per_share"] == "4"
    assert payload["snapshot"]["book_value_per_share"] == "9"
    assert payload["snapshot"]["recurring_dividends_per_share"] == "2"


async def test_endpoint_reports_unavailable_without_instrument(tmp_path: Path) -> None:
    from app.api.dependencies import get_fundamentals_service, get_opportunity_service
    from app.main import create_app

    service = await build(tmp_path, StubProvider())

    class StubOpportunity:
        async def opportunity(self, ticker: str) -> SimpleNamespace:
            metric = SimpleNamespace(value=None, sources=[])
            return SimpleNamespace(
                instrument=None,
                metrics=SimpleNamespace(
                    shares_outstanding=metric,
                    earnings_per_share=metric,
                    book_value_per_share=metric,
                    dividends_12m=metric,
                ),
            )

    app = create_app()
    app.dependency_overrides[get_fundamentals_service] = lambda: service
    app.dependency_overrides[get_opportunity_service] = StubOpportunity
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/v1/assets/TEST4/fundamentals")

    assert response.status_code == 200
    assert response.json()["snapshot"]["unavailable_reason"] == (
        "Corporate name is required to resolve CVM filings"
    )


async def test_periods_carry_the_share_count_of_their_own_year(tmp_path: Path) -> None:
    """A change in shares outstanding must stay visible across the series."""
    provider = StubProvider(
        {
            ("dfp", 2024): archive(2024, [period(date(2024, 12, 31))], shares="900"),
            ("dfp", 2023): archive(2023, [period(date(2023, 12, 31))], shares="1000"),
        }
    )
    config = settings(tmp_path)
    cache = CacheStore(sqlite_enabled=False, sqlite_path=config.sqlite_cache_path)
    await cache.startup()
    service = FundamentalsService(
        config,
        cache,
        provider,
        StubInternationalProvider(),  # type: ignore[arg-type]
    )

    snapshot = await service.snapshot("TEST4", COMPANY, reference=date(2024, 12, 31))

    by_year = {item.period_end.year: item.shares_outstanding for item in snapshot.periods}
    assert by_year[2023] == Decimal("1000")
    assert by_year[2024] == Decimal("900")


async def test_stock_splits_do_not_look_like_shareholder_dilution(tmp_path: Path) -> None:
    provider = StubProvider(
        {
            ("dfp", 2024): archive(2024, [period(date(2024, 12, 31))], shares="2000"),
            ("dfp", 2023): archive(2023, [period(date(2023, 12, 31))], shares="1000"),
        }
    )
    service = await build(tmp_path, provider)

    snapshot = await service.snapshot("TEST4", COMPANY, reference=date(2024, 12, 31))

    by_year = {item.period_end.year: item.shares_outstanding for item in snapshot.periods}
    assert by_year[2023] == Decimal("2000")
    assert by_year[2024] == Decimal("2000")


async def test_uses_filing_eps_when_historical_share_capital_is_unavailable(
    tmp_path: Path,
) -> None:
    old_period = period(date(2023, 12, 31))
    old_period = replace(
        old_period,
        accounts={**old_period.accounts, "3.99.01.01": Decimal("3")},
    )
    provider = StubProvider(
        {
            ("dfp", 2024): archive(2024, [period(date(2024, 12, 31))], shares="100"),
            ("dfp", 2023): StatementArchive(
                kind=StatementKind.ANNUAL,
                year=2023,
                periods={CNPJ: [old_period]},
                share_capital={},
            ),
        }
    )
    service = await build(tmp_path, provider)

    snapshot = await service.snapshot("TEST4", COMPANY, reference=date(2024, 12, 31))

    by_year = {item.period_end.year: item.shares_outstanding for item in snapshot.periods}
    assert by_year[2023] == Decimal("100")
    assert by_year[2024] == Decimal("100")


async def test_periods_do_not_invent_a_historical_share_count(tmp_path: Path) -> None:
    provider = StubProvider(
        {("dfp", 2024): archive(2024, [period(date(2020, 12, 31))], shares="900")}
    )
    config = settings(tmp_path)
    cache = CacheStore(sqlite_enabled=False, sqlite_path=config.sqlite_cache_path)
    await cache.startup()
    service = FundamentalsService(
        config,
        cache,
        provider,
        StubInternationalProvider(),  # type: ignore[arg-type]
    )

    snapshot = await service.snapshot("TEST4", COMPANY, reference=date(2024, 12, 31))

    assert snapshot.periods[0].shares_outstanding is None


def _international_listing() -> InternationalListing:
    return InternationalListing(
        ticker="AAPL",
        currency="USD",
        price=Decimal("331.09"),
        price_to_earnings=Decimal("39.84"),
        price_to_book=Decimal("45.86"),
        market_capitalization=Decimal("4883614590000"),
        equity=Decimal("106491000000"),
        shares_outstanding=Decimal("14673000000"),
    )


async def test_a_foreign_listing_falls_back_to_public_statements(tmp_path: Path) -> None:
    """No Brazilian issuer is absent from the CVM, so an unmatched ticker is foreign."""
    international = StubInternationalProvider(_international_listing())
    service = await build(tmp_path, StubProvider(), international)

    snapshot = await service.snapshot("AAPL", None)

    assert snapshot.unavailable_reason is None
    assert snapshot.periods[0].source == "investidor10"
    assert international.calls == ["AAPL"]


async def test_the_cvm_reason_survives_when_no_statements_are_found(tmp_path: Path) -> None:
    """A ticker neither source knows must explain the original failure."""
    service = await build(tmp_path, StubProvider(), StubInternationalProvider(None))

    snapshot = await service.snapshot("NOPE", None)

    assert snapshot.unavailable_reason == "Corporate name is required to resolve CVM filings"


async def test_an_unreachable_public_source_does_not_mask_the_cvm_reason(
    tmp_path: Path,
) -> None:
    class FailingProvider(StubInternationalProvider):
        async def listing(self, ticker: str) -> InternationalListing | None:
            raise httpx.ConnectTimeout("unreachable")

    service = await build(tmp_path, StubProvider(), FailingProvider())

    snapshot = await service.snapshot("AAPL", None)

    assert snapshot.unavailable_reason == "Corporate name is required to resolve CVM filings"
