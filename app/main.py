import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api import router
from app.cache import CacheStore
from app.config import get_settings
from app.core.errors import register_error_handlers
from app.income import IncomeEventService, IncomeEventStore
from app.income.sources import (
    FundamentusIncomeSource,
    FundosNetIncomeSource,
    OfficialCompanyIncomeSource,
    StatusInvestIncomeSource,
)
from app.scrapers import FundamentusClient, FundamentusScraper
from app.services import (
    AssetService,
    BcbBankProvider,
    BcbMacroProvider,
    FixedIncomeValuationService,
    FundamentalsService,
    HistoricalQuoteService,
    InstrumentDataService,
    OpportunityService,
    QualityFactsService,
)


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
    income_event_store = IncomeEventStore(settings.sqlite_cache_path)
    await income_event_store.startup()
    app.state.income_event_service = IncomeEventService(
        income_event_store,
        [
            OfficialCompanyIncomeSource(settings),
            FundosNetIncomeSource(settings),
            FundamentusIncomeSource(asset_service),
            StatusInvestIncomeSource(settings),
        ],
    )
    app.state.opportunity_service = OpportunityService(asset_service, settings)
    instrument_data_service = InstrumentDataService(settings)
    app.state.instrument_data_service = instrument_data_service
    # Bulk directory warming is deliberately detached from readiness. Search
    # serves the current memory snapshot while SEC/brapi refresh in background.
    app.state.instrument_directory_warm_task = asyncio.create_task(
        instrument_data_service.warm_directory()
    )
    app.state.fixed_income_valuation_service = FixedIncomeValuationService(settings, cache)
    app.state.historical_quote_service = HistoricalQuoteService(settings, cache)
    fundamentals_service = FundamentalsService(settings, cache)
    app.state.fundamentals_service = fundamentals_service
    app.state.quality_facts_service = QualityFactsService(
        fundamentals_service,
        instrument_data_service,
        app.state.opportunity_service,
        macro_provider=BcbMacroProvider(settings),
        bank_provider=BcbBankProvider(settings),
    )
    try:
        yield
    finally:
        await instrument_data_service.close()
        await income_event_store.close()
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
