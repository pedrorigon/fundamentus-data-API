from __future__ import annotations

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
from app.scrapers.anbima_fixed_income import SOURCE_ANBIMA, AnbimaDebentureProvider
from app.scrapers.b3_fixed_income import B3FixedIncomeProvider

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
    ) -> None:
        self.settings = settings
        self.cache = cache
        self.provider = provider or AnbimaDebentureProvider(settings)
        if fallback_providers is not None:
            self.fallback_providers = fallback_providers
        elif provider is None:
            self.fallback_providers = (
                AnbimaCreditProvider(settings),
                B3FixedIncomeProvider(settings),
            )
        else:
            # Tests and callers that provide a primary source explicitly should
            # not trigger additional network sources unless they opt in.
            self.fallback_providers = ()

    async def resolve(
        self,
        request: FixedIncomeValuationRequest,
    ) -> FixedIncomeValuationResponse:
        resolved: dict[str, list[FixedIncomeValuation]] = {
            identifier: [] for identifier in request.identifiers
        }
        for target in request.dates:
            prices = await self._latest_prices(target, set(request.identifiers))
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
                "No public indicative price was found in ANBIMA or B3 sources for the requested "
                "date; contractual terms are required to calculate an accrued value."
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
    ) -> dict[str, _ResolvedPrice]:
        resolved: dict[str, _ResolvedPrice] = {}
        providers = ((self.provider, SOURCE_ANBIMA),) + tuple(
            (provider, getattr(provider, "source", SOURCE_ANBIMA))
            for provider in self.fallback_providers
        )
        for provider, source in providers:
            remaining = identifiers - resolved.keys()
            if not remaining:
                break
            scoped = callable(getattr(provider, "prices_for", None))
            for days_ago in range(LOOKBACK_DAYS + 1):
                reference = target - timedelta(days=days_ago)
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
        identifier_key = ",".join(sorted(identifiers))
        key = f"fixed-income:{source}:{reference.isoformat()}:{identifier_key}"
        cached, found = await self.cache.get(key)
        if found:
            return _cached_prices(cached)
        prices: dict[str, Decimal] = {}
        try:
            prices_for = getattr(provider, "prices_for", None)
            if callable(prices_for):
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
