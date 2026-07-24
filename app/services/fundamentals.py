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
)
from app.parsers.cvm_statements import StatementPeriod
from app.parsers.normalizers import normalize_ticker
from app.scrapers.cvm_open_data import CvmOpenDataProvider, StatementKind
from app.services.company_matching import match_company
from app.services.fundamentals_math import build_period, ratio, trailing_twelve_months

SOURCE_CVM = "cvm"
STATUS_VALID = "valid"
STATUS_MISSING = "missing_data"

_CACHE_PREFIX = "fundamentals"
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

    async def snapshot(
        self,
        ticker: str,
        corporate_name: str | None = None,
        *,
        reference: date | None = None,
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

        shares = await self._shares(match.cnpj, today)
        shares_by_year = await self._shares_by_year(match.cnpj, today)
        periods = _unique_periods(
            build_period(
                statement,
                # Each period carries the count reported for its own year, so a
                # change in shares outstanding stays visible across the series.
                shares_outstanding=shares_by_year.get(statement.period_end.year, shares),
            )
            for statement in statements
        )
        ttm = trailing_twelve_months(periods)
        return FundamentalsSnapshot(
            ticker=normalized,
            cnpj=match.cnpj,
            company_name=match.company_name,
            sector=await self._sector(match.cnpj),
            periods=periods,
            trailing_twelve_months=ttm,
            shares_outstanding=shares,
            provenance=_provenance(ttm, shares, confidence=match.confidence),
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

    async def _archives(self, reference: date) -> list[dict[str, list[StatementPeriod]]]:
        years = range(reference.year, reference.year - self.settings.fundamentals_history_years, -1)
        annual = await asyncio.gather(*(self._archive(StatementKind.ANNUAL, y) for y in years))
        quarterly = await self._archive(StatementKind.QUARTERLY, reference.year)
        archives = [archive for archive in annual if archive]
        if quarterly:
            archives.append(quarterly)
        return archives

    async def _archive(
        self, kind: StatementKind, year: int
    ) -> dict[str, list[StatementPeriod]] | None:
        key = f"{_CACHE_PREFIX}:archive:{kind.value}:{year}"
        cached, hit = await self.cache.get(key)
        if hit:
            return _decode_periods(cached)

        archive = await self.provider.statements(kind, year)
        if archive is None:
            await self.cache.set(key, {}, self.settings.market_data_ttl_seconds)
            return None
        await self.cache.set(
            key,
            _encode_periods(archive.periods),
            self.settings.fundamentals_statements_ttl_seconds,
        )
        await self.cache.set(
            f"{_CACHE_PREFIX}:shares:{year}",
            {cnpj: str(value.outstanding_shares) for cnpj, value in archive.share_capital.items()},
            self.settings.fundamentals_statements_ttl_seconds,
        )
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
        key = f"{_CACHE_PREFIX}:registry"
        cached, hit = await self.cache.get(key)
        if not hit:
            registry = await self.provider.registry()
            cached = {code: entry.sector for code, entry in registry.items() if entry.sector}
            await self.cache.set(key, cached, self.settings.company_registry_ttl_seconds)
        if isinstance(cached, dict):
            sector = cached.get(cnpj)
            return str(sector) if sector else None
        return None

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
    }
    reference = period.period_end if period else None
    retrieved = datetime.now(UTC).date()
    return [
        FieldProvenance(
            field_name=name,
            value=value,
            selected_source=SOURCE_CVM if value is not None else None,
            reference_date=reference,
            retrieved_at=retrieved,
            confidence=weight if value is not None else Decimal("0"),
            status=STATUS_VALID if value is not None else STATUS_MISSING,
        )
        for name, value in fields.items()
    ]


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
                "consolidated": item.consolidated,
                "accounts": {code: str(value) for code, value in item.accounts.items()},
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
                depreciation=(
                    Decimal(str(item["depreciation"])) if item["depreciation"] is not None else None
                ),
            )
            for item in values
            if isinstance(item, dict)
        ]
    return decoded or None
