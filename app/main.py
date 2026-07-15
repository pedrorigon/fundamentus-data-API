from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api import router
from app.cache import CacheStore
from app.config import get_settings
from app.core.errors import register_error_handlers
from app.scrapers import FundamentusClient, FundamentusScraper
from app.services import AssetService, InstrumentDataService, OpportunityService


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    cache = CacheStore(
        sqlite_enabled=settings.sqlite_cache_enabled,
        sqlite_path=settings.sqlite_cache_path,
    )
    client = FundamentusClient(settings)
    await cache.startup()
    await client.startup()
    scraper = FundamentusScraper(client, settings)
    asset_service = AssetService(scraper, cache, settings)
    app.state.asset_service = asset_service
    app.state.opportunity_service = OpportunityService(asset_service, settings)
    app.state.instrument_data_service = InstrumentDataService(settings)
    try:
        yield
    finally:
        await client.shutdown()
        await cache.close()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description="Local HTTP API for Brazilian and international market data.",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )
    register_error_handlers(app)
    app.include_router(router)
    return app


app = create_app()
