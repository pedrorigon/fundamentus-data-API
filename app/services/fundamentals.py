"""Resolve multi-year fundamentals for a ticker from CVM open data.

The service owns the fallback chain, provenance and caching. Statement archives
are large and shared across every ticker, so they are cached per year and reused
rather than downloaded per request.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import UTC, date, datetime
from decimal import Decimal

from app.cache import CacheStore
from app.config import Settings
from app.core.errors import InvalidTickerError
from app.models.fundamentals import (
    FieldProvenance,
    FinancialPeriod,
    FundamentalsSnapshot,
    PeerGroup,
    SectorCompany,
)
from app.parsers.cvm_statements import StatementPeriod
from app.parsers.normalizers import normalize_ticker
from app.scrapers.cvm_open_data import CvmOpenDataProvider, StatementKind
from app.services.company_matching import match_company
from app.services.fundamentals_math import build_period, ratio, trailing_twelve_months

SOURCE_CVM = "cvm"
STATUS_VALID = "valid"
STATUS_MISSING = "missing_data"

_CACHE_PREFIX = "fundamentals:v6"
_MINIMUM_PEERS = 3


class FundamentalsService:
    def __init__(
        self,
        settings: Settings,
        cache: CacheStore,
        provider: CvmOpenDataProvider | None = None,
    ) -> None:
        self.settings = settings
        self.cache = cache
        self.provider = provider or CvmOpenDataProvider(settings)
        self._decoded_archives: dict[
            tuple[StatementKind, int],
            dict[str, list[StatementPeriod]] | None,
        ] = {}

    async def snapshot(
        self,
        ticker: str,
        corporate_name: str | None = None,
        *,
        reference: date | None = None,
        reference_shares: Decimal | None = None,
        earnings_per_share: Decimal | None = None,
        book_value_per_share: Decimal | None = None,
        recurring_dividends_per_share: Decimal | None = None,
        supplemental_sources: dict[str, str] | None = None,
    ) -> FundamentalsSnapshot:
        normalized = self._normalized(ticker)
        if not corporate_name:
            return _empty(normalized, "Corporate name is required to resolve CVM filings")

        today = reference or datetime.now(UTC).date()
        archives = await self._archives(today)
        if not archives:
            return _empty(normalized, "CVM statement archives are unavailable")

        candidates = {
            cnpj: periods[0].company_name
            for archive in archives
            for cnpj, periods in archive.items()
            if periods
        }
        match = match_company(corporate_name, candidates)
        if match is None:
            return _empty(normalized, "No CVM filing matched this company")

        statements = [
            statement for archive in archives for statement in archive.get(match.cnpj, [])
        ]
        if not statements:
            return _empty(normalized, "No statement periods available for this company")

        sector = await self._sector(match.cnpj)
        shares = await self._shares(match.cnpj, today)
        shares_by_year = await self._shares_by_year(match.cnpj, today)
        raw_periods = _unique_periods(
            build_period(
                statement,
                shares_outstanding=shares_by_year.get(statement.period_end.year),
                sector=sector,
            )
            for statement in statements
        )
        raw_ttm = trailing_twelve_months(raw_periods)
        implied_shares = _implied_share_reference(
            raw_ttm,
            earnings_per_share,
            book_value_per_share,
        )
        market_reference = reference_shares or implied_shares
        share_factor = _share_scale_factor(shares, market_reference)
        normalized_shares = market_reference or _scaled(shares, share_factor)
        share_history = {
            period.period_end.year: period.shares_outstanding
            for period in raw_periods
            if period.shares_outstanding is not None
        }
        share_history.update(
            {
                year: scaled
                for year, value in shares_by_year.items()
                if (scaled := _scaled(value, share_factor)) is not None
            }
        )
        normalized_shares_by_year = _normalize_share_history(share_history)
        periods = [
            period.model_copy(
                update={
                    "shares_outstanding": normalized_shares_by_year.get(
                        period.period_end.year,
                        period.shares_outstanding,
                    )
                }
            )
            for period in raw_periods
        ]
        ttm = trailing_twelve_months(periods)
        shares_source = SOURCE_CVM
        if implied_shares:
            shares_source = "derived_public_indicators"
        if reference_shares:
            shares_source = "fundamentus"
        return FundamentalsSnapshot(
            ticker=normalized,
            cnpj=match.cnpj,
            company_name=match.company_name,
            sector=sector,
            periods=periods,
            trailing_twelve_months=ttm,
            shares_outstanding=normalized_shares,
            earnings_per_share=earnings_per_share,
            book_value_per_share=book_value_per_share,
            recurring_dividends_per_share=recurring_dividends_per_share,
            provenance=_provenance(
                ttm,
                normalized_shares,
                confidence=match.confidence,
                shares_source=shares_source,
                supplemental={
                    "earnings_per_share": earnings_per_share,
                    "book_value_per_share": book_value_per_share,
                    "recurring_dividends_per_share": recurring_dividends_per_share,
                },
                supplemental_sources=supplemental_sources or {},
            ),
        )

    async def peer_group(
        self,
        sector: str,
        snapshots: list[tuple[FundamentalsSnapshot, Decimal]],
    ) -> PeerGroup:
        """Aggregate sector medians from resolved snapshots and current prices."""
        samples: dict[str, list[Decimal]] = {
            "price_to_earnings": [],
            "price_to_book": [],
            "ev_to_ebitda": [],
            "ev_to_ebit": [],
            "price_to_free_cash_flow": [],
            "earnings_yield": [],
            "free_cash_flow_yield": [],
        }
        tickers: list[str] = []
        for snapshot, price in snapshots:
            period = snapshot.trailing_twelve_months
            if period is None or price <= 0 or not snapshot.shares_outstanding:
                continue
            tickers.append(snapshot.ticker)
            _collect_multiples(samples, period, price, snapshot.shares_outstanding)

        if len(tickers) < _MINIMUM_PEERS:
            return PeerGroup(
                sector=sector,
                tickers=tickers,
                sample_size=len(tickers),
                unavailable_reason="Not enough peers with comparable fundamentals",
            )
        return PeerGroup(
            sector=sector,
            tickers=tickers,
            sample_size=len(tickers),
            median_price_to_earnings=_median(samples["price_to_earnings"]),
            median_price_to_book=_median(samples["price_to_book"]),
            median_ev_to_ebitda=_median(samples["ev_to_ebitda"]),
            median_ev_to_ebit=_median(samples["ev_to_ebit"]),
            median_price_to_free_cash_flow=_median(samples["price_to_free_cash_flow"]),
            median_earnings_yield=_median(samples["earnings_yield"]),
            median_free_cash_flow_yield=_median(samples["free_cash_flow_yield"]),
        )

    async def sector_universe(
        self,
        *,
        reference: date | None = None,
    ) -> dict[str, list[SectorCompany]]:
        """Group every filing company by its registered sector.

        Relative valuation needs a comparison base drawn from the whole market
        rather than from the tickers a caller happens to hold, so the universe
        comes from the CVM archives themselves.
        """
        today = reference or datetime.now(UTC).date()
        archives = await self._archives(today)
        if not archives:
            return {}
        sectors = await self._sectors()

        latest: dict[str, StatementPeriod] = {}
        for archive in archives:
            for cnpj, periods in archive.items():
                for statement in periods:
                    current = latest.get(cnpj)
                    if current is None or statement.period_end > current.period_end:
                        latest[cnpj] = statement

        universe: dict[str, list[SectorCompany]] = {}
        for cnpj, statement in latest.items():
            sector = sectors.get(cnpj)
            if not sector:
                continue
            period = build_period(statement, sector=sector)
            if period.equity is None or period.equity <= 0:
                continue
            universe.setdefault(sector, []).append(
                SectorCompany(
                    cnpj=cnpj,
                    company_name=statement.company_name,
                    sector=sector,
                    period=period,
                )
            )
        return universe

    async def _sectors(self) -> dict[str, str]:
        key = f"{_CACHE_PREFIX}:registry"
        cached, hit = await self.cache.get(key)
        if not hit:
            registry = await self.provider.registry()
            cached = {code: entry.sector for code, entry in registry.items() if entry.sector}
            await self.cache.set(key, cached, self.settings.company_registry_ttl_seconds)
        if not isinstance(cached, dict):
            return {}
        return {str(cnpj): str(sector) for cnpj, sector in cached.items() if sector}

    async def _archives(self, reference: date) -> list[dict[str, list[StatementPeriod]]]:
        years = range(reference.year, reference.year - self.settings.fundamentals_history_years, -1)
        annual = await asyncio.gather(*(self._archive(StatementKind.ANNUAL, y) for y in years))
        quarterly = await asyncio.gather(
            self._archive(StatementKind.QUARTERLY, reference.year),
            self._archive(StatementKind.QUARTERLY, reference.year - 1),
        )
        archives = [archive for archive in annual if archive]
        archives.extend(archive for archive in quarterly if archive)
        return archives

    async def _archive(
        self, kind: StatementKind, year: int
    ) -> dict[str, list[StatementPeriod]] | None:
        archive_key = (kind, year)
        if archive_key in self._decoded_archives:
            return self._decoded_archives[archive_key]

        key = f"{_CACHE_PREFIX}:archive:{kind.value}:{year}"
        cached, hit = await self.cache.get(key, memory=False)
        if hit:
            decoded = _decode_periods(cached)
            self._decoded_archives[archive_key] = decoded
            return decoded

        archive = await self.provider.statements(kind, year)
        if archive is None:
            await self.cache.set(
                key,
                {},
                self.settings.market_data_ttl_seconds,
                memory=False,
            )
            self._decoded_archives[archive_key] = None
            return None
        await self.cache.set(
            key,
            _encode_periods(archive.periods),
            self.settings.fundamentals_statements_ttl_seconds,
            memory=False,
        )
        await self.cache.set(
            f"{_CACHE_PREFIX}:shares:{year}",
            {cnpj: str(value.outstanding_shares) for cnpj, value in archive.share_capital.items()},
            self.settings.fundamentals_statements_ttl_seconds,
        )
        self._decoded_archives[archive_key] = archive.periods
        return archive.periods

    async def _shares_by_year(self, cnpj: str, reference: date) -> dict[int, Decimal]:
        """Share counts reported for each cached year."""
        series: dict[int, Decimal] = {}
        for year in range(
            reference.year,
            reference.year - self.settings.fundamentals_history_years,
            -1,
        ):
            cached, hit = await self.cache.get(f"{_CACHE_PREFIX}:shares:{year}")
            if not hit or not isinstance(cached, dict) or cnpj not in cached:
                continue
            try:
                value = Decimal(str(cached[cnpj]))
            except (ArithmeticError, ValueError):
                continue
            if value > 0:
                series[year] = value
        return series

    async def _shares(self, cnpj: str, reference: date) -> Decimal | None:
        for year in range(reference.year, reference.year - 3, -1):
            cached, hit = await self.cache.get(f"{_CACHE_PREFIX}:shares:{year}")
            if hit and isinstance(cached, dict) and cnpj in cached:
                try:
                    value = Decimal(str(cached[cnpj]))
                except (ArithmeticError, ValueError):
                    continue
                if value > 0:
                    return value
        return None

    async def _sector(self, cnpj: str) -> str | None:
        return (await self._sectors()).get(cnpj)

    def _normalized(self, ticker: str) -> str:
        try:
            return normalize_ticker(ticker)
        except ValueError as exc:
            raise InvalidTickerError(ticker=ticker) from exc


def _collect_multiples(
    samples: dict[str, list[Decimal]],
    period: FinancialPeriod,
    price: Decimal,
    shares: Decimal,
) -> None:
    market_cap = price * shares
    enterprise = market_cap + (period.net_debt or Decimal("0"))
    _append(samples["price_to_earnings"], ratio(market_cap, period.net_income))
    _append(samples["price_to_book"], ratio(market_cap, period.equity))
    _append(samples["ev_to_ebitda"], ratio(enterprise, period.ebitda))
    _append(samples["ev_to_ebit"], ratio(enterprise, period.ebit))
    _append(samples["price_to_free_cash_flow"], ratio(market_cap, period.free_cash_flow))
    _append(samples["earnings_yield"], ratio(period.net_income, market_cap))
    _append(samples["free_cash_flow_yield"], ratio(period.free_cash_flow, market_cap))


def _append(target: list[Decimal], value: Decimal | None) -> None:
    if value is not None:
        target.append(value)


def _median(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal("2")


def _unique_periods(periods: Iterable[FinancialPeriod]) -> list[FinancialPeriod]:
    """Keep one period per end date, preferring consolidated statements."""
    selected: dict[date, FinancialPeriod] = {}
    for period in periods:
        existing = selected.get(period.period_end)
        if existing is None or (period.consolidated and not existing.consolidated):
            selected[period.period_end] = period
    return [selected[key] for key in sorted(selected)]


def _provenance(
    period: FinancialPeriod | None,
    shares: Decimal | None,
    *,
    confidence: str,
    shares_source: str = SOURCE_CVM,
    supplemental: dict[str, Decimal | None] | None = None,
    supplemental_sources: dict[str, str] | None = None,
) -> list[FieldProvenance]:
    weight = Decimal("0.95") if confidence == "high" else Decimal("0.8")
    fields = {
        "revenue_ttm": period.revenue if period else None,
        "ebit_ttm": period.ebit if period else None,
        "ebitda_ttm": period.ebitda if period else None,
        "net_income_ttm": period.net_income if period else None,
        "equity": period.equity if period else None,
        "net_debt": period.net_debt if period else None,
        "free_cash_flow_ttm": period.free_cash_flow if period else None,
        "shares_outstanding": shares,
        **(supplemental or {}),
    }
    sources = supplemental_sources or {}
    reference = period.period_end if period else None
    retrieved = datetime.now(UTC).date()
    return [
        FieldProvenance(
            field_name=name,
            value=value,
            selected_source=(
                shares_source if name == "shares_outstanding" else sources.get(name, SOURCE_CVM)
            )
            if value is not None
            else None,
            reference_date=reference,
            retrieved_at=retrieved,
            confidence=weight if value is not None else Decimal("0"),
            status=STATUS_VALID if value is not None else STATUS_MISSING,
        )
        for name, value in fields.items()
    ]


def _share_scale_factor(
    reported: Decimal | None,
    reference: Decimal | None,
) -> Decimal:
    """Reconcile issuer-specific CVM share units against a market reference."""
    if reported is None or reported <= 0 or reference is None or reference <= 0:
        return Decimal("1")
    candidates = (Decimal("0.001"), Decimal("1"), Decimal("1000"))
    factor = min(candidates, key=lambda candidate: abs(reported * candidate - reference))
    relative_error = abs(reported * factor - reference) / reference
    return factor if relative_error <= Decimal("0.1") else Decimal("1")


def _implied_share_reference(
    period: FinancialPeriod | None,
    earnings_per_share: Decimal | None,
    book_value_per_share: Decimal | None,
) -> Decimal | None:
    """Derive a corroborating share count from independently sourced ratios."""
    if period is None:
        return None
    candidates = [
        implied
        for total, per_share_value in (
            (period.net_income, earnings_per_share),
            (period.equity, book_value_per_share),
        )
        if total is not None
        and total > 0
        and per_share_value is not None
        and per_share_value > 0
        and (implied := total / per_share_value) > 0
    ]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    if max(candidates) / min(candidates) > Decimal("1.2"):
        return candidates[0]
    return _median(candidates)


def _scaled(value: Decimal | None, factor: Decimal) -> Decimal | None:
    return value * factor if value is not None else None


def _normalize_share_history(series: dict[int, Decimal]) -> dict[int, Decimal]:
    """Express historical counts on the latest split-adjusted share basis."""
    normalized = dict(series)
    years = sorted(normalized)
    split_factors = (
        Decimal("1.5"),
        Decimal("2"),
        Decimal("3"),
        Decimal("4"),
        Decimal("5"),
        Decimal("10"),
    )
    for previous_year, current_year in zip(years, years[1:], strict=False):
        previous = normalized[previous_year]
        current = normalized[current_year]
        if previous <= 0 or current <= 0:
            continue
        ratio = current / previous
        candidates = (*split_factors, *(Decimal("1") / factor for factor in split_factors))
        split = min(candidates, key=lambda candidate: abs(ratio - candidate))
        if abs(ratio - split) / split > Decimal("0.06"):
            continue
        for year in years:
            if year <= previous_year:
                normalized[year] *= split
    return normalized


def _empty(ticker: str, reason: str) -> FundamentalsSnapshot:
    return FundamentalsSnapshot(ticker=ticker, unavailable_reason=reason)


def _encode_periods(
    periods: dict[str, list[StatementPeriod]],
) -> dict[str, list[dict[str, object]]]:
    return {
        cnpj: [
            {
                "cnpj": item.cnpj,
                "cvm_code": item.cvm_code,
                "company_name": item.company_name,
                "reference_date": item.reference_date.isoformat(),
                "period_start": item.period_start.isoformat() if item.period_start else None,
                "period_end": item.period_end.isoformat(),
                "published_at": item.published_at.isoformat() if item.published_at else None,
                "consolidated": item.consolidated,
                "accounts": {code: str(value) for code, value in item.accounts.items()},
                "account_labels": dict(item.account_labels),
                "depreciation": str(item.depreciation) if item.depreciation is not None else None,
            }
            for item in values
        ]
        for cnpj, values in periods.items()
    }


def _decode_periods(payload: object) -> dict[str, list[StatementPeriod]] | None:
    if not isinstance(payload, dict) or not payload:
        return None
    decoded: dict[str, list[StatementPeriod]] = {}
    for cnpj, values in payload.items():
        if not isinstance(values, list):
            continue
        decoded[str(cnpj)] = [
            StatementPeriod(
                cnpj=str(item["cnpj"]),
                cvm_code=str(item["cvm_code"]),
                company_name=str(item["company_name"]),
                reference_date=date.fromisoformat(str(item["reference_date"])),
                period_start=(
                    date.fromisoformat(str(item["period_start"])) if item["period_start"] else None
                ),
                period_end=date.fromisoformat(str(item["period_end"])),
                consolidated=bool(item["consolidated"]),
                accounts={
                    str(code): Decimal(str(value)) for code, value in dict(item["accounts"]).items()
                },
                account_labels={
                    str(code): str(label)
                    for code, label in dict(item.get("account_labels") or {}).items()
                },
                depreciation=(
                    Decimal(str(item["depreciation"])) if item["depreciation"] is not None else None
                ),
                published_at=(
                    date.fromisoformat(str(item["published_at"]))
                    if item.get("published_at")
                    else None
                ),
            )
            for item in values
            if isinstance(item, dict)
        ]
    return decoded or None
