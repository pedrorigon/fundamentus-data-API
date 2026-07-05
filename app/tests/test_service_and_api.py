import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
import pytest

from app import __version__
from app.api.dependencies import get_asset_service
from app.cache import CacheStore
from app.config import Settings
from app.main import create_app
from app.services import AssetService

FIXTURES = Path(__file__).parent / "fixtures"


class FakeScraper:
    def __init__(self, *, delay: float = 0.0, fail: bool = False) -> None:
        self.delay = delay
        self.fail = fail
        self.details_calls = 0
        self.dividend_calls = 0

    def details_url(self, ticker: str) -> str:
        return f"https://example.local/detalhes.php?papel={ticker}&h=1"

    async def fetch_details(self, ticker: str) -> str:
        self.details_calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("upstream failed")
        name = "itub4_details.html" if ticker == "ITUB4" else "wege3_details.html"
        return (FIXTURES / name).read_text(encoding="iso-8859-1")

    async def fetch_dividends(self, ticker: str) -> str:
        self.dividend_calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        name = "itub4_dividends.html" if ticker == "ITUB4" else "wege3_dividends.html"
        return (FIXTURES / name).read_text(encoding="iso-8859-1")


async def make_service(scraper: FakeScraper, *, ttl: int = 60) -> AssetService:
    with TemporaryDirectory() as tmp:
        settings = Settings(
            sqlite_cache_enabled=False,
            sqlite_cache_path=Path(tmp) / "cache.sqlite3",
            market_data_ttl_seconds=ttl,
            fundamentals_ttl_seconds=ttl,
            dividends_ttl_seconds=ttl,
        )
        cache = CacheStore(sqlite_enabled=False, sqlite_path=settings.sqlite_cache_path)
        await cache.startup()
        return AssetService(scraper, cache, settings)


@pytest.mark.asyncio
async def test_sqlite_cache_serializes_model_lists() -> None:
    scraper = FakeScraper()
    with TemporaryDirectory() as tmp:
        settings = Settings(
            sqlite_cache_enabled=True,
            sqlite_cache_path=Path(tmp) / "cache.sqlite3",
            dividends_ttl_seconds=60,
        )
        cache = CacheStore(sqlite_enabled=True, sqlite_path=settings.sqlite_cache_path)
        await cache.startup()
        service = AssetService(scraper, cache, settings)

        dividends, cached = await service.get_dividends("ITUB4")
        assert dividends
        assert cached is False
        await cache.close()

        reopened = CacheStore(sqlite_enabled=True, sqlite_path=settings.sqlite_cache_path)
        await reopened.startup()
        cached_payload, hit = await reopened.get("dividends:ITUB4")
        await reopened.close()

        assert hit is True
        assert isinstance(cached_payload, list)
        assert isinstance(cached_payload[0], dict)


@pytest.mark.asyncio
async def test_cache_and_force_refresh() -> None:
    scraper = FakeScraper()
    service = await make_service(scraper)

    details1, cached1 = await service.get_details("ITUB4")
    details2, cached2 = await service.get_details("ITUB4")
    details3, cached3 = await service.get_details("ITUB4", force_refresh=True)

    assert details1.ticker == details2.ticker == details3.ticker == "ITUB4"
    assert cached1 is False
    assert cached2 is True
    assert cached3 is False
    assert scraper.details_calls == 2


@pytest.mark.asyncio
async def test_singleflight_coalesces_concurrent_scrapes() -> None:
    scraper = FakeScraper(delay=0.05)
    service = await make_service(scraper)

    results = await asyncio.gather(*[service.get_details("ITUB4") for _ in range(8)])
    assert all(details.ticker == "ITUB4" for details, _cached in results)
    assert scraper.details_calls == 1


@pytest.mark.asyncio
async def test_endpoint_contract() -> None:
    scraper = FakeScraper()
    service = await make_service(scraper)
    app = create_app()
    app.dependency_overrides[get_asset_service] = lambda: service

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        health = await client.get("/health")
        assert health.status_code == 200
        assert health.json()["version"] == __version__

        response = await client.get(
            "/v1/assets/ITUB4",
            params={"period": "future", "as_of": "2026-07-04"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["ticker"] == "ITUB4"
        assert payload["details"]["quote"] == "42.74"
        assert payload["dividends"]
        assert payload["dividends"][0]["is_future_payment"] is True

        details = await client.get("/v1/assets/WEGE3/details")
        assert details.status_code == 200
        assert details.json()["ticker"] == "WEGE3"

        dividends = await client.get(
            "/v1/assets/ITUB4/dividends",
            params={"period": "past", "as_of": "2026-07-04"},
        )
        assert dividends.status_code == 200
        assert all(item["is_future_payment"] is False for item in dividends.json())

        batch = await client.get("/v1/assets", params={"tickers": "WEGE3,ITUB4"})
        assert batch.status_code == 200
        assert batch.json()["count"] == 2

        metrics = await client.get("/metrics")
        assert metrics.status_code == 200
        assert "fundamentus_api_uptime_seconds" in metrics.text


@pytest.mark.asyncio
async def test_endpoint_errors_are_consistent() -> None:
    scraper = FakeScraper()
    service = await make_service(scraper)
    app = create_app()
    app.dependency_overrides[get_asset_service] = lambda: service

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        invalid = await client.get("/v1/assets/BAD TICKER")
        assert invalid.status_code == 400
        assert invalid.json()["error"]["code"] == "INVALID_TICKER"

        too_many = await client.get(
            "/v1/assets",
            params={"tickers": ",".join(f"ABCD{i}" for i in range(30))},
        )
        assert too_many.status_code == 429
        assert too_many.json()["error"]["code"] == "LOCAL_RATE_LIMIT"
