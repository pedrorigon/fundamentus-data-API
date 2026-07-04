import asyncio
import random
import time
from urllib.parse import urlencode

import httpx

from app.config import Settings
from app.core.errors import CircuitBreakerOpenError, UpstreamUnavailableError
from app.core.metrics import metrics


class CircuitBreaker:
    def __init__(self, *, failure_threshold: int, recovery_seconds: float) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self._failures = 0
        self._opened_until = 0.0
        self._lock = asyncio.Lock()

    async def before_request(self) -> None:
        async with self._lock:
            if self._opened_until > time.monotonic():
                raise CircuitBreakerOpenError()
            if self._opened_until:
                self._opened_until = 0.0
                self._failures = 0

    async def success(self) -> None:
        async with self._lock:
            self._failures = 0
            self._opened_until = 0.0

    async def failure(self) -> None:
        async with self._lock:
            self._failures += 1
            if self._failures >= self.failure_threshold:
                self._opened_until = time.monotonic() + self.recovery_seconds
                metrics.inc("circuit_breaker_opened")


class FundamentusClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: httpx.AsyncClient | None = None
        self._semaphore = asyncio.Semaphore(settings.upstream_concurrency)
        self._breaker = CircuitBreaker(
            failure_threshold=settings.circuit_breaker_failures,
            recovery_seconds=settings.circuit_breaker_recovery_seconds,
        )
        self._interval_lock = asyncio.Lock()
        self._last_request_at = 0.0

    async def startup(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=self.settings.fundamentus_base_url,
            headers={
                "User-Agent": self.settings.user_agent,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.4",
            },
            timeout=httpx.Timeout(self.settings.request_timeout_seconds),
            limits=httpx.Limits(
                max_connections=self.settings.max_connections,
                max_keepalive_connections=self.settings.max_keepalive_connections,
            ),
            follow_redirects=True,
        )

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def get_html(self, path: str, params: dict[str, str]) -> str:
        if self._client is None:
            raise RuntimeError("FundamentusClient was not started")

        await self._breaker.before_request()
        async with self._semaphore:
            for attempt in range(1, self.settings.retry_attempts + 1):
                try:
                    await self._respect_interval()
                    response = await self._client.get(path, params=params)
                    if response.status_code >= 500 or response.status_code == 429:
                        raise httpx.HTTPStatusError(
                            "transient upstream status",
                            request=response.request,
                            response=response,
                        )
                    response.raise_for_status()
                    await self._breaker.success()
                    metrics.inc("upstream_requests")
                    return response.content.decode("iso-8859-1", errors="replace")
                except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                    if attempt >= self.settings.retry_attempts:
                        await self._breaker.failure()
                        metrics.inc("upstream_failures")
                        raise UpstreamUnavailableError(retryable=True) from exc
                    delay = self.settings.retry_backoff_seconds * (2 ** (attempt - 1))
                    delay += random.uniform(0, delay / 2)
                    await asyncio.sleep(delay)

        raise UpstreamUnavailableError(retryable=True)

    async def _respect_interval(self) -> None:
        async with self._interval_lock:
            elapsed = time.monotonic() - self._last_request_at
            wait_for = self.settings.upstream_min_interval_seconds - elapsed
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            self._last_request_at = time.monotonic()


class FundamentusScraper:
    def __init__(self, client: FundamentusClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings

    def details_url(self, ticker: str) -> str:
        query = urlencode({"papel": ticker, "h": "1"})
        return f"{self.settings.fundamentus_base_url}/detalhes.php?{query}"

    def dividends_url(self, ticker: str) -> str:
        return f"{self.settings.fundamentus_base_url}/proventos.php?{urlencode({'papel': ticker})}"

    async def fetch_details(self, ticker: str) -> str:
        return await self.client.get_html("/detalhes.php", {"papel": ticker, "h": "1"})

    async def fetch_dividends(self, ticker: str) -> str:
        return await self.client.get_html("/proventos.php", {"papel": ticker})
