from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from time import monotonic

import httpx

from app.config import Settings

SOURCE_ANBIMA_FEED = "anbima_feed"
TOKEN_EXPIRY_MARGIN_SECONDS = 30


@dataclass(frozen=True)
class _AccessToken:
    value: str
    expires_at: float


class AnbimaFeedProvider:
    """Authenticated ANBIMA indicative prices, including retained history."""

    identifier_scoped = False
    full_history = True
    source = SOURCE_ANBIMA_FEED

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        client_id = settings.anbima_feed_client_id
        client_secret = settings.anbima_feed_client_secret
        if client_id is None or client_secret is None:
            raise ValueError("ANBIMA Feed credentials are required")
        self._client_id = client_id.get_secret_value()
        self._client_secret = client_secret.get_secret_value()
        if not self._client_id or not self._client_secret:
            raise ValueError("ANBIMA Feed credentials cannot be empty")
        self._clock = clock
        self._token: _AccessToken | None = None
        self._token_lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            base_url=settings.anbima_feed_base_url.rstrip("/"),
            timeout=httpx.Timeout(settings.request_timeout_seconds),
            transport=transport,
            headers={"User-Agent": settings.user_agent},
        )

    async def prices(self, reference: date) -> dict[str, Decimal]:
        attempt = 0
        while True:
            token = await self._access_token()
            response = await self._client.get(
                "/feed/precos-indices/v1/debentures/mercado-secundario",
                params={"data": reference.isoformat()},
                headers={"client_id": self._client_id, "access_token": token},
            )
            if response.status_code != httpx.codes.UNAUTHORIZED or attempt == 1:
                response.raise_for_status()
                return parse_anbima_feed_prices(response.json())
            self._expire_token(token)
            attempt += 1

    async def close(self) -> None:
        await self._client.aclose()

    async def _access_token(self) -> str:
        current = self._token
        if current is not None and current.expires_at > self._clock():
            return current.value
        async with self._token_lock:
            current = self._token
            if current is not None and current.expires_at > self._clock():
                return current.value
            response = await self._client.post(
                "/oauth/access-token",
                auth=httpx.BasicAuth(self._client_id, self._client_secret),
                json={"grant_type": "client_credentials"},
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("ANBIMA Feed returned an invalid token payload")
            value = payload.get("access_token")
            expires_in = payload.get("expires_in")
            if not isinstance(value, str) or not value or not isinstance(expires_in, (int, float)):
                raise ValueError("ANBIMA Feed returned an invalid access token")
            lifetime = max(float(expires_in) - TOKEN_EXPIRY_MARGIN_SECONDS, 1.0)
            self._token = _AccessToken(value=value, expires_at=self._clock() + lifetime)
            return value

    def _expire_token(self, value: str) -> None:
        if self._token is not None and self._token.value == value:
            self._token = None


def parse_anbima_feed_prices(payload: object) -> dict[str, Decimal]:
    if not isinstance(payload, list):
        return {}
    prices: dict[str, Decimal] = {}
    for row in payload:
        if not isinstance(row, dict):
            continue
        identifier = row.get("codigo_ativo")
        if not isinstance(identifier, str) or not identifier.strip():
            continue
        unit_price = _positive_decimal(row.get("pu_retificado")) or _positive_decimal(row.get("pu"))
        if unit_price is not None:
            prices[identifier.strip().upper()] = unit_price
    return prices


def _positive_decimal(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None
