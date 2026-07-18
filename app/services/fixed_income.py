from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import httpx

from app.cache import CacheStore
from app.config import Settings
from app.models import (
    FixedIncomeValuation,
    FixedIncomeValuationRequest,
    FixedIncomeValuationResponse,
    ValuationMethod,
)
from app.scrapers.anbima_fixed_income import SOURCE_ANBIMA, AnbimaDebentureProvider

LOOKBACK_DAYS = 7


class FixedIncomeValuationService:
    def __init__(
        self,
        settings: Settings,
        cache: CacheStore,
        provider: AnbimaDebentureProvider | None = None,
    ) -> None:
        self.settings = settings
        self.cache = cache
        self.provider = provider or AnbimaDebentureProvider(settings)

    async def resolve(
        self,
        request: FixedIncomeValuationRequest,
    ) -> FixedIncomeValuationResponse:
        resolved: dict[str, list[FixedIncomeValuation]] = {
            identifier: [] for identifier in request.identifiers
        }
        for target in request.dates:
            prices, reference = await self._latest_prices(target)
            for identifier in request.identifiers:
                unit_price = prices.get(identifier)
                if unit_price is not None and reference is not None:
                    resolved[identifier].append(
                        FixedIncomeValuation(
                            identifier=identifier,
                            reference_date=reference,
                            unit_price=unit_price,
                            source=SOURCE_ANBIMA,
                            method=ValuationMethod.indicative,
                        )
                    )
        unavailable = [identifier for identifier, values in resolved.items() if not values]
        return FixedIncomeValuationResponse(valuations=resolved, unavailable=unavailable)

    async def _latest_prices(self, target: date) -> tuple[dict[str, Decimal], date | None]:
        for days_ago in range(LOOKBACK_DAYS + 1):
            reference = target - timedelta(days=days_ago)
            prices = await self._prices(reference)
            if prices:
                return prices, reference
        return {}, None

    async def _prices(self, reference: date) -> dict[str, Decimal]:
        key = f"fixed-income:anbima-debentures:{reference.isoformat()}"
        cached, found = await self.cache.get(key)
        if found:
            return _cached_prices(cached)
        try:
            prices = await self.provider.prices(reference)
        except (httpx.HTTPError, ValueError):
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
    return {
        str(code): Decimal(str(price))
        for code, price in value.items()
        if isinstance(code, str) and isinstance(price, (str, int, float))
    }
