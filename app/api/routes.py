import asyncio
import hmac
from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Response, status
from fastapi.responses import PlainTextResponse

from app import __version__
from app.api.dependencies import (
    get_asset_service,
    get_fixed_income_valuation_service,
    get_fundamentals_service,
    get_historical_quote_service,
    get_income_event_service,
    get_instrument_data_service,
    get_opportunity_service,
    get_quality_facts_service,
)
from app.config import Settings, get_settings
from app.core.errors import UnauthorizedCacheInvalidationError
from app.core.metrics import metrics
from app.income import IncomeEventService
from app.models import (
    AssetDetails,
    AssetResponse,
    BatchAssetResponse,
    CacheInvalidationRequest,
    CacheInvalidationResponse,
    Dividend,
    DividendPeriod,
    FixedIncomeValuationRequest,
    FixedIncomeValuationResponse,
    FundamentalsBatchRequest,
    FundamentalsBatchResponse,
    FundamentalsResponse,
    HealthResponse,
    HistoricalQuoteRequest,
    HistoricalQuoteResponse,
    IncomeEventBatchRequest,
    IncomeEventBatchResponse,
    IncomeEventChangesResponse,
    IncomeEventRefreshRequest,
    IncomeEventRefreshResponse,
    InstrumentDataResponse,
    InstrumentMetadata,
    InstrumentSearchResponse,
    InstrumentType,
    OpportunityResponse,
    QualityFactsRequest,
    QualityFactsResponse,
    SectorUniverseResponse,
)
from app.services import (
    AssetService,
    FixedIncomeValuationService,
    FundamentalsService,
    HistoricalQuoteService,
    InstrumentDataService,
    OpportunityService,
    QualityFactsService,
)

router = APIRouter()

AssetServiceDep = Annotated[AssetService, Depends(get_asset_service)]
OpportunityServiceDep = Annotated[OpportunityService, Depends(get_opportunity_service)]
InstrumentDataServiceDep = Annotated[InstrumentDataService, Depends(get_instrument_data_service)]
FundamentalsServiceDep = Annotated[FundamentalsService, Depends(get_fundamentals_service)]
QualityFactsServiceDep = Annotated[QualityFactsService, Depends(get_quality_facts_service)]
FixedIncomeServiceDep = Annotated[
    FixedIncomeValuationService,
    Depends(get_fixed_income_valuation_service),
]
HistoricalQuoteServiceDep = Annotated[HistoricalQuoteService, Depends(get_historical_quote_service)]
IncomeEventServiceDep = Annotated[IncomeEventService, Depends(get_income_event_service)]
ForceRefreshQuery = Annotated[bool, Query()]
AsOfQuery = Annotated[date | None, Query()]
DividendPeriodQuery = Annotated[DividendPeriod, Query()]
IncludeDetailsQuery = Annotated[bool, Query()]
IncludeDividendsQuery = Annotated[bool, Query()]
TickersQuery = Annotated[str, Query(description="Comma-separated tickers, e.g. WEGE3,ITUB4")]
CacheTokenHeader = Annotated[str | None, Header(alias="X-Cache-Token")]


@router.post(
    "/v2/income-events/refresh",
    response_model=IncomeEventRefreshResponse,
    tags=["income-events"],
)
async def refresh_income_events(
    payload: IncomeEventRefreshRequest,
    service: IncomeEventServiceDep,
    x_cache_token: CacheTokenHeader = None,
) -> IncomeEventRefreshResponse:
    _require_refresh_authorization(x_cache_token)
    return await service.refresh(payload)


@router.post(
    "/v2/income-events/batch",
    response_model=IncomeEventBatchResponse,
    tags=["income-events"],
)
async def resolve_income_events_batch(
    payload: IncomeEventBatchRequest,
    service: IncomeEventServiceDep,
    response: Response,
) -> IncomeEventBatchResponse:
    result = await service.batch(payload)
    _income_cache_headers(response, result.cursor)
    return result


@router.get(
    "/v2/income-events/changes",
    response_model=IncomeEventChangesResponse,
    tags=["income-events"],
)
async def income_event_changes(
    service: IncomeEventServiceDep,
    response: Response,
    cursor: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
) -> IncomeEventChangesResponse:
    result = await service.changes(cursor, limit)
    _income_cache_headers(response, result.cursor)
    return result


def _require_refresh_authorization(provided: str | None) -> None:
    settings = get_settings()
    if (
        settings.environment.lower() in {"local", "test"}
        and not _configured_maintenance_token(settings)
    ):
        return
    _require_maintenance_token(provided)


def _income_cache_headers(response: Response, cursor: int) -> None:
    _cache_headers(response)
    response.headers["ETag"] = f'W/"income-{cursor}"'


def _require_maintenance_token(provided: str | None) -> None:
    configured = _configured_maintenance_token(get_settings())
    if not configured or not provided or not hmac.compare_digest(provided, configured):
        raise UnauthorizedCacheInvalidationError()


def _configured_maintenance_token(settings: Settings) -> str | None:
    configured_secret = settings.cache_invalidate_token
    if configured_secret is None:
        return None
    return configured_secret.get_secret_value().strip() or None


def _authorize_force_refresh(force_refresh: bool, provided: str | None) -> None:
    if force_refresh:
        _require_maintenance_token(provided)


@router.post(
    "/v1/fixed-income/valuations/resolve",
    response_model=FixedIncomeValuationResponse,
    tags=["fixed-income"],
)
async def resolve_fixed_income_valuations(
    payload: FixedIncomeValuationRequest,
    service: FixedIncomeServiceDep,
) -> FixedIncomeValuationResponse:
    return await service.resolve(payload)


@router.post(
    "/v1/equities/historical-quotes/resolve",
    response_model=HistoricalQuoteResponse,
    tags=["equities"],
)
async def resolve_historical_quotes(
    payload: HistoricalQuoteRequest,
    service: HistoricalQuoteServiceDep,
) -> HistoricalQuoteResponse:
    return await service.resolve(payload)


@router.get("/v1/instruments/{ticker}")
async def get_instrument(
    ticker: str,
    service: OpportunityServiceDep,
) -> InstrumentMetadata | None:
    return await service.instrument(ticker)


@router.get("/v2/instruments/search", response_model=InstrumentSearchResponse, tags=["instruments"])
async def search_instruments(
    service: InstrumentDataServiceDep,
    q: str = Query(min_length=1, max_length=80),
    limit: int = Query(default=20, ge=1, le=50),
) -> InstrumentSearchResponse:
    """Search the locally observed instrument directory without upstream calls."""
    return await service.search(q, limit=limit)


@router.get("/v2/instruments/{ticker}", tags=["instruments"])
async def get_instrument_data(
    ticker: str,
    service: InstrumentDataServiceDep,
    instrument_type: InstrumentType | None = None,
) -> InstrumentDataResponse:
    return await service.get(ticker, instrument_type)


@router.get("/v1/assets/{ticker}/opportunity")
async def get_opportunity(
    ticker: str,
    service: OpportunityServiceDep,
) -> OpportunityResponse:
    return await service.opportunity(ticker)


@router.post(
    "/v1/quality/facts:resolve",
    response_model=QualityFactsResponse,
    tags=["quality"],
)
async def resolve_quality_facts(
    payload: QualityFactsRequest,
    service: QualityFactsServiceDep,
    response: Response,
) -> QualityFactsResponse:
    """Resolve provenanced quality evidence for a bounded asset batch."""
    _cache_headers(response)
    return await service.resolve(payload)


@router.get(
    "/v1/assets/{ticker}/fundamentals",
    response_model=FundamentalsResponse,
    tags=["assets"],
)
async def get_fundamentals(
    ticker: str,
    fundamentals: FundamentalsServiceDep,
    opportunity: OpportunityServiceDep,
    response: Response,
) -> FundamentalsResponse:
    """Return multi-year fundamentals resolved from CVM filings."""
    _cache_headers(response)
    return await _resolve_fundamentals(ticker, fundamentals, opportunity)


async def _resolve_fundamentals(
    ticker: str,
    fundamentals: FundamentalsService,
    opportunity: OpportunityService,
) -> FundamentalsResponse:
    """Resolve one ticker, shared by the single and the batch routes."""
    opportunity_data = await opportunity.opportunity(ticker)
    instrument = opportunity_data.instrument
    metrics = opportunity_data.metrics
    snapshot = await fundamentals.snapshot(
        ticker,
        instrument.name if instrument else None,
        reference_shares=metrics.shares_outstanding.value,
        earnings_per_share=metrics.earnings_per_share.value,
        book_value_per_share=metrics.book_value_per_share.value,
        recurring_dividends_per_share=metrics.dividends_12m.value,
        supplemental_sources={
            "earnings_per_share": ",".join(metrics.earnings_per_share.sources),
            "book_value_per_share": ",".join(metrics.book_value_per_share.sources),
            "recurring_dividends_per_share": ",".join(metrics.dividends_12m.sources),
        },
    )
    return FundamentalsResponse(
        ticker=snapshot.ticker,
        snapshot=snapshot,
        refreshed_at=datetime.now(UTC).date(),
    )


@router.post(
    "/v1/assets/fundamentals:resolve",
    response_model=FundamentalsBatchResponse,
    tags=["assets"],
)
async def resolve_fundamentals_batch(
    payload: FundamentalsBatchRequest,
    fundamentals: FundamentalsServiceDep,
    opportunity: OpportunityServiceDep,
    response: Response,
) -> FundamentalsBatchResponse:
    """Resolve fundamentals for a bounded batch in one round.

    A caller holding a portfolio would otherwise issue one request per ticker,
    each paying its own round trip. The tickers are independent, so they are
    resolved together and returned in the order they were asked for.
    """
    _cache_headers(response)
    resolved = await asyncio.gather(
        *(_resolve_fundamentals(ticker, fundamentals, opportunity) for ticker in payload.tickers)
    )
    return FundamentalsBatchResponse(
        assets=list(resolved),
        refreshed_at=datetime.now(UTC).date(),
    )


@router.get(
    "/v1/sectors/universe",
    response_model=SectorUniverseResponse,
    tags=["assets"],
)
async def get_sector_universe(
    fundamentals: FundamentalsServiceDep,
    response: Response,
) -> SectorUniverseResponse:
    """Return every filing company grouped by its registered sector.

    Consumers use this to build peer medians from the whole market instead of
    from the tickers they happen to hold.
    """
    _cache_headers(response)
    return SectorUniverseResponse(
        sectors=await fundamentals.sector_universe(),
        refreshed_at=datetime.now(UTC).date(),
    )


def _cache_headers(response: Response, *, force_refresh: bool = False) -> None:
    settings = get_settings()
    if force_refresh:
        response.headers["Cache-Control"] = "no-store"
    else:
        response.headers["Cache-Control"] = (
            f"private, max-age={settings.cache_headers_max_age_seconds}"
        )


@router.get("/health", response_model=HealthResponse, tags=["system"])
async def health() -> HealthResponse:
    settings = get_settings()
    return HealthResponse(
        status="ok",
        version=__version__,
        environment=settings.environment,
        checks={
            "bind_host": settings.bind_host,
            "sqlite_cache_enabled": settings.sqlite_cache_enabled,
        },
    )


@router.get("/metrics", response_class=PlainTextResponse, tags=["system"])
async def prometheus_metrics() -> str:
    return metrics.render_prometheus()


@router.get("/v1/assets/{ticker}", response_model=AssetResponse, tags=["assets"])
async def get_asset(
    ticker: str,
    response: Response,
    service: AssetServiceDep,
    include_details: IncludeDetailsQuery = True,
    include_dividends: IncludeDividendsQuery = True,
    period: DividendPeriodQuery = DividendPeriod.all,
    as_of: AsOfQuery = None,
    force_refresh: ForceRefreshQuery = False,
    x_cache_token: CacheTokenHeader = None,
) -> AssetResponse:
    _authorize_force_refresh(force_refresh, x_cache_token)
    _cache_headers(response, force_refresh=force_refresh)
    metrics.inc("asset_endpoint_requests")
    return await service.get_asset(
        ticker,
        include_details=include_details,
        include_dividends=include_dividends,
        period=period,
        as_of=as_of,
        force_refresh=force_refresh,
    )


@router.get("/v1/assets/{ticker}/details", response_model=AssetDetails, tags=["assets"])
async def get_details(
    ticker: str,
    response: Response,
    service: AssetServiceDep,
    force_refresh: ForceRefreshQuery = False,
    x_cache_token: CacheTokenHeader = None,
) -> AssetDetails:
    _authorize_force_refresh(force_refresh, x_cache_token)
    _cache_headers(response, force_refresh=force_refresh)
    metrics.inc("details_endpoint_requests")
    details, _cached = await service.get_details(ticker, force_refresh=force_refresh)
    return details


@router.get("/v1/assets/{ticker}/dividends", response_model=list[Dividend], tags=["assets"])
async def get_dividends(
    ticker: str,
    response: Response,
    service: AssetServiceDep,
    period: DividendPeriodQuery = DividendPeriod.all,
    as_of: AsOfQuery = None,
    force_refresh: ForceRefreshQuery = False,
    x_cache_token: CacheTokenHeader = None,
) -> list[Dividend]:
    _authorize_force_refresh(force_refresh, x_cache_token)
    _cache_headers(response, force_refresh=force_refresh)
    metrics.inc("dividends_endpoint_requests")
    dividends, _cached = await service.get_dividends(
        ticker,
        period=period,
        as_of=as_of,
        force_refresh=force_refresh,
    )
    return dividends


@router.get("/v1/assets", response_model=BatchAssetResponse, tags=["assets"])
async def get_assets_batch(
    response: Response,
    service: AssetServiceDep,
    tickers: TickersQuery,
    include_details: IncludeDetailsQuery = True,
    include_dividends: IncludeDividendsQuery = False,
    period: DividendPeriodQuery = DividendPeriod.all,
    as_of: AsOfQuery = None,
    force_refresh: ForceRefreshQuery = False,
    x_cache_token: CacheTokenHeader = None,
) -> BatchAssetResponse:
    _authorize_force_refresh(force_refresh, x_cache_token)
    _cache_headers(response, force_refresh=force_refresh)
    metrics.inc("batch_endpoint_requests")
    ticker_list = [item.strip() for item in tickers.split(",") if item.strip()]
    results = await service.get_batch(
        ticker_list,
        include_details=include_details,
        include_dividends=include_dividends,
        period=period,
        as_of=as_of,
        force_refresh=force_refresh,
    )
    return BatchAssetResponse(count=len(results), results=results)


@router.post(
    "/v1/cache/invalidate",
    response_model=CacheInvalidationResponse,
    status_code=status.HTTP_200_OK,
    tags=["cache"],
)
async def invalidate_cache(
    payload: CacheInvalidationRequest,
    service: AssetServiceDep,
    x_cache_token: CacheTokenHeader = None,
) -> CacheInvalidationResponse:
    provided = x_cache_token or payload.token
    _require_maintenance_token(provided)

    if payload.ticker:
        ticker = service.normalize_ticker(payload.ticker)
        await service.cache.invalidate(f"details:{ticker}")
        await service.cache.invalidate(f"dividends:{ticker}")
        return CacheInvalidationResponse(invalidated=True, ticker=ticker)

    await service.cache.invalidate()
    return CacheInvalidationResponse(invalidated=True, ticker=None)
