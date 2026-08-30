from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, cast

import httpx

from app.cache import CacheStore
from app.config import Settings
from app.models import (
    FixedIncomeValuation,
    FixedIncomeValuationRequest,
    FixedIncomeValuationResponse,
    ValuationMethod,
)
from app.scrapers.anbima_credit import AnbimaCreditProvider
from app.scrapers.anbima_feed import AnbimaFeedProvider
from app.scrapers.anbima_fixed_income import SOURCE_ANBIMA, AnbimaDebentureProvider
from app.scrapers.b3_fixed_income import B3FixedIncomeProvider
from app.scrapers.snd_fixed_income import SndDebentureTradeProvider
from app.services.singleflight import SingleFlight

LOOKBACK_DAYS = 7


@dataclass(frozen=True)
class _ResolvedPrice:
    reference_date: date
    unit_price: Decimal
    source: str


class FixedIncomeValuationService:
    def __init__(
        self,
        settings: Settings,
        cache: CacheStore,
        provider: AnbimaDebentureProvider | None = None,
        fallback_providers: tuple[object, ...] | None = None,
        history_provider: object | None = None,
    ) -> None:
        self.settings = settings
        self.cache = cache
        self.provider = provider or AnbimaDebentureProvider(settings)
        self.history_provider = history_provider
        if history_provider is None and provider is None and settings.anbima_feed_configured:
            self.history_provider = AnbimaFeedProvider(settings)
        if fallback_providers is not None:
            self.fallback_providers = fallback_providers
        elif provider is None:
            self.fallback_providers = (
                B3FixedIncomeProvider(settings),
                SndDebentureTradeProvider(settings),
                AnbimaCreditProvider(settings),
            )
        else:
            # Tests and callers that provide a primary source explicitly should
            # not trigger additional network sources unless they opt in.
            self.fallback_providers = ()
        self.singleflight = SingleFlight()

    async def close(self) -> None:
        close = getattr(self.history_provider, "close", None)
        if callable(close):
            await close()

    async def resolve(
        self,
        request: FixedIncomeValuationRequest,
    ) -> FixedIncomeValuationResponse:
        resolved: dict[str, list[FixedIncomeValuation]] = {
            identifier: [] for identifier in request.identifiers
        }
        semaphore = asyncio.Semaphore(max(1, self.settings.upstream_concurrency))
        public_history_start = _month_start_before(
            date.today(),
            self.settings.fixed_income_public_history_months,
        )

        async def resolve_target(target: date) -> dict[str, _ResolvedPrice]:
            requires_full_history = target < public_history_start
            async with semaphore:
                return await self._latest_prices(
                    target,
                    set(request.identifiers),
                    full_history_only=requires_full_history,
                )

        prices_by_target = await asyncio.gather(
            *(resolve_target(target) for target in request.dates)
        )
        for target, prices in zip(request.dates, prices_by_target, strict=True):
            for identifier in request.identifiers:
                resolved_price = prices.get(identifier)
                if resolved_price is not None:
                    resolved[identifier].append(
                        FixedIncomeValuation(
                            identifier=identifier,
                            requested_date=target,
                            reference_date=resolved_price.reference_date,
                            unit_price=resolved_price.unit_price,
                            source=resolved_price.source,
                            method=ValuationMethod.indicative,
                        )
                    )
        unavailable = [identifier for identifier, values in resolved.items() if not values]
        unavailable_reasons = {
            identifier: (
                "No observed market price was found in ANBIMA, B3, or SND sources for the "
                "requested date; contractual terms are required to calculate an accrued value."
            )
            for identifier in unavailable
        }
        return FixedIncomeValuationResponse(
            valuations=resolved,
            unavailable=unavailable,
            unavailable_reasons=unavailable_reasons,
        )

    async def _latest_prices(
        self,
        target: date,
        identifiers: set[str],
        *,
        full_history_only: bool,
    ) -> dict[str, _ResolvedPrice]:
        resolved: dict[str, _ResolvedPrice] = {}
        configured_providers = (
            (
                ((self.history_provider, getattr(self.history_provider, "source", SOURCE_ANBIMA)),)
                if self.history_provider is not None
                else ()
            )
            + ((self.provider, SOURCE_ANBIMA),)
            + tuple(
                (provider, getattr(provider, "source", SOURCE_ANBIMA))
                for provider in self.fallback_providers
            )
        )
        providers = (
            tuple(item for item in configured_providers if _has_full_history(item[0]))
            if full_history_only
            else configured_providers
        )
        for provider, source in providers:
            remaining = identifiers - resolved.keys()
            if not remaining:
                break
            scoped = _is_identifier_scoped(provider)
            for days_ago in range(LOOKBACK_DAYS + 1):
                reference = target - timedelta(days=days_ago)
                if reference.weekday() >= 5:
                    continue
                prices = await self._prices(provider, source, reference, remaining)
                for identifier, unit_price in prices.items():
                    if identifier in remaining:
                        resolved[identifier] = _ResolvedPrice(
                            reference_date=reference,
                            unit_price=unit_price,
                            source=source,
                        )
                remaining = identifiers - resolved.keys()
                if not remaining:
                    break
                if prices and not scoped:
                    break
        return resolved

    async def _prices(
        self,
        provider: object,
        source: str,
        reference: date,
        identifiers: set[str],
    ) -> dict[str, Decimal]:
        scoped = _is_identifier_scoped(provider)
        identifier_key = ",".join(sorted(identifiers)) if scoped else "*"
        key = f"fixed-income:{source}:{reference.isoformat()}:{identifier_key}"
        cached, found = await self.cache.get(key)
        if found:
            return _cached_prices(cached)

        async def load() -> dict[str, Decimal]:
            prices: dict[str, Decimal] = {}
            try:
                prices_for = getattr(provider, "prices_for", None)
                if scoped and callable(prices_for):
                    candidate = await prices_for(reference, identifiers)
                else:
                    candidate = await cast(Any, provider).prices(reference)
                prices = cast(dict[str, Decimal], candidate)
            except (httpx.HTTPError, TypeError, ValueError):
                prices = {}
            ttl = (
                self.settings.fixed_income_history_ttl_seconds
                if reference < date.today()
                else self.settings.fixed_income_current_ttl_seconds
            )
            await self.cache.set(key, {code: str(price) for code, price in prices.items()}, ttl)
            return prices

        return await self.singleflight.run(key, load)


def _is_identifier_scoped(provider: object) -> bool:
    configured = getattr(provider, "identifier_scoped", None)
    if isinstance(configured, bool):
        return configured
    return callable(getattr(provider, "prices_for", None))


def _has_full_history(provider: object | None) -> bool:
    return bool(provider is not None and getattr(provider, "full_history", False) is True)


def _month_start_before(reference: date, months: int) -> date:
    month_index = reference.year * 12 + reference.month - 1 - max(months, 0)
    return date(month_index // 12, month_index % 12 + 1, 1)


def _cached_prices(value: object) -> dict[str, Decimal]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Decimal] = {}
    for code, price in value.items():
        if not isinstance(code, str) or not isinstance(price, (str, int, float)):
            continue
        try:
            parsed = Decimal(str(price))
        except (InvalidOperation, ValueError):
            continue
        if parsed.is_finite() and parsed > 0:
            result[code] = parsed
    return result
