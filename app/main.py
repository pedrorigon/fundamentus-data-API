import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI

from app import __version__
from app.api import router
from app.assessment import AssessmentSnapshotService, AssessmentStore, BootstrapAssessmentWorker
from app.assessment.routes import router as assessment_router
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


async def _noop() -> None:
    """Provide a typed no-op for cleanup of resources not yet constructed."""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    cache: CacheStore | None = None
    client: FundamentusClient | None = None
    income_event_store: IncomeEventStore | None = None
    income_event_service: IncomeEventService | None = None
    instrument_data_service: InstrumentDataService | None = None
    instrument_directory_warm_task: asyncio.Task[None] | None = None
    fixed_income_valuation_service: FixedIncomeValuationService | None = None
    assessment_store: AssessmentStore | None = None
    bootstrap_worker: BootstrapAssessmentWorker | None = None
    maintenance_task: asyncio.Task[None] | None = None
    startup_complete = False
    cleanup_error: Exception | None = None

    async def attempt_cleanup(action: Callable[[], Awaitable[None]]) -> None:
        """Run one cleanup action while allowing the remaining actions to run."""
        nonlocal cleanup_error
        try:
            await action()
        except Exception as exc:
            if cleanup_error is None:
                cleanup_error = exc

    async def close_instrument_resources() -> None:
        """Stop both the detached wrapper task and its service-owned task."""
        if instrument_directory_warm_task is not None:
            if not instrument_directory_warm_task.done():
                instrument_directory_warm_task.cancel()
            # Retrieving the result also handles a warm-up that failed before
            # shutdown, preventing an unhandled task exception warning.
            await asyncio.gather(instrument_directory_warm_task, return_exceptions=True)
        if instrument_data_service is not None:
            await instrument_data_service.close()

    try:
        cache = CacheStore(
            sqlite_enabled=settings.sqlite_cache_enabled,
            sqlite_path=settings.sqlite_cache_path,
            # ``getattr`` keeps lightweight lifecycle test doubles and older
            # embedding integrations compatible while Settings supplies the
            # configured production default.
            memory_cache_max_entries=getattr(settings, "memory_cache_max_entries", 4096),
        )
        await cache.startup()
        client = FundamentusClient(settings)
        await client.startup()
        scraper = FundamentusScraper(client, settings)
        asset_service = AssetService(scraper, cache, settings)
        app.state.asset_service = asset_service
        income_event_store = IncomeEventStore(
            settings.sqlite_cache_path,
            # ``getattr`` keeps lightweight lifecycle test doubles compatible
            # while Settings supplies the configured shared store.
            database_url=(
                getattr(settings, "income_store_url", None)
                or getattr(settings, "database_url", None)
            ),
        )
        await income_event_store.startup()
        status_income_source = StatusInvestIncomeSource(settings)
        income_event_service = IncomeEventService(
            income_event_store,
            [
                OfficialCompanyIncomeSource(settings),
                FundosNetIncomeSource(settings, status_source=status_income_source),
                FundamentusIncomeSource(asset_service),
                status_income_source,
            ],
            snapshot_overlap_days=settings.income_snapshot_overlap_days,
            refresh_ttl_seconds=settings.income_refresh_ttl_seconds,
        )
        await getattr(income_event_service, "startup", _noop)()
        app.state.income_event_service = income_event_service
        app.state.opportunity_service = OpportunityService(
            asset_service, settings, income_store=income_event_store
        )
        instrument_data_service = InstrumentDataService(settings)
        app.state.instrument_data_service = instrument_data_service
        # Bulk directory warming is deliberately detached from readiness. Search
        # serves the current memory snapshot while SEC/brapi refresh in background.
        instrument_directory_warm_task = asyncio.create_task(
            instrument_data_service.warm_directory()
        )
        app.state.instrument_directory_warm_task = instrument_directory_warm_task
        fixed_income_valuation_service = FixedIncomeValuationService(settings, cache)
        app.state.fixed_income_valuation_service = fixed_income_valuation_service
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
        assessment_store = AssessmentStore(
            settings.resolved_assessment_sqlite_path,
            database_url=settings.database_url,
            default_lease_seconds=settings.assessment_lease_seconds,
            max_attempts=settings.assessment_max_attempts,
        )
        await assessment_store.startup()
        retention_cutoff = datetime.now(UTC) - timedelta(
            days=settings.assessment_period_history_days
        )
        maintenance = getattr(assessment_store, "maintenance", None)
        if callable(maintenance):
            await maintenance(retention_cutoff)
        else:
            # Keep lightweight embedding/test doubles compatible with the
            # historical cleanup-only lifecycle.
            await assessment_store.cleanup_before(retention_cutoff)
        app.state.assessment_store = assessment_store
        app.state.assessment_service = AssessmentSnapshotService(
            assessment_store,
            app.state.opportunity_service,
            fundamentals_service,
            app.state.quality_facts_service,
            period_history_days=settings.assessment_period_history_days,
            retry_backoff_seconds=settings.assessment_retry_backoff_seconds,
            bootstrap_retry_after_seconds=getattr(
                settings, "assessment_bootstrap_retry_after_seconds", 2
            ),
        )

        # Bootstrap work is optional for lightweight embedding doubles used by
        # integrations and lifecycle tests.  The production store implements
        # every queue method, so it always starts the durable worker.
        if all(
            callable(getattr(assessment_store, name, None))
            for name in (
                "enqueue_bootstrap",
                "claim_bootstrap_due",
                "complete_bootstrap",
                "claim",
                "release_bootstrap",
                "release_claim",
                "renew_claim",
                "renew_bootstrap_lease",
            )
        ):
            bootstrap_worker = BootstrapAssessmentWorker(
                assessment_store,
                app.state.assessment_service,
                concurrency=getattr(settings, "assessment_bootstrap_concurrency", 2),
                poll_interval_seconds=getattr(
                    settings, "assessment_bootstrap_poll_interval_seconds", 1.0
                ),
                lease_seconds=getattr(settings, "assessment_bootstrap_lease_seconds", 120),
                build_timeout_seconds=getattr(
                    settings, "assessment_bootstrap_build_timeout_seconds", 1200.0
                ),
                retry_after_seconds=getattr(
                    settings, "assessment_bootstrap_retry_after_seconds", 2
                ),
            )
            await bootstrap_worker.start()
            app.state.assessment_bootstrap_worker = bootstrap_worker
            wakeup = getattr(app.state.assessment_service, "set_bootstrap_wakeup", None)
            if callable(wakeup):
                wakeup(bootstrap_worker.wake)

        async def maintain_resources() -> None:
            interval = max(1.0, float(getattr(settings, "maintenance_interval_seconds", 60)))
            while True:
                await asyncio.sleep(interval)
                if cache is not None:
                    await cache.cleanup_expired(limit=100)
                if assessment_store is not None:
                    cutoff = datetime.now(UTC) - timedelta(
                        days=settings.assessment_period_history_days
                    )
                    assessment_maintenance = getattr(assessment_store, "maintenance", None)
                    if callable(assessment_maintenance):
                        await assessment_maintenance(cutoff)
                    else:
                        await assessment_store.cleanup_before(cutoff)

        maintenance_task = asyncio.create_task(maintain_resources())
        startup_complete = True
        yield
    finally:
        if maintenance_task is not None:
            maintenance_task.cancel()
            await asyncio.gather(maintenance_task, return_exceptions=True)
        if bootstrap_worker is not None:
            await attempt_cleanup(bootstrap_worker.stop)
        # Stop the detached directory warm-up before closing other resources.
        # Its provider tasks can start network I/O as soon as shutdown yields
        # to the event loop; cancelling them first prevents transports from
        # outliving the lifespan that owns them.
        await attempt_cleanup(close_instrument_resources)
        await attempt_cleanup(assessment_store.close if assessment_store is not None else _noop)
        await attempt_cleanup(
            fixed_income_valuation_service.close
            if fixed_income_valuation_service is not None
            else _noop
        )
        await attempt_cleanup(
            getattr(income_event_service, "close", _noop)
            if income_event_service is not None
            else _noop
        )
        await attempt_cleanup(income_event_store.close if income_event_store is not None else _noop)
        await attempt_cleanup(client.shutdown if client is not None else _noop)
        await attempt_cleanup(cache.close if cache is not None else _noop)
        if startup_complete and cleanup_error is not None:
            raise cleanup_error


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
    app.include_router(assessment_router)
    return app


app = create_app()
