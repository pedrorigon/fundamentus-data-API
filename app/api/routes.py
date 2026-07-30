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
    get_instrument_data_service,
    get_opportunity_service,
    get_quality_facts_service,
)
from app.config import get_settings
from app.core.errors import UnauthorizedCacheInvalidationError
from app.core.metrics import metrics
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
    FundamentalsResponse,
    HealthResponse,
    HistoricalQuoteRequest,
    HistoricalQuoteResponse,
    InstrumentDataResponse,
    InstrumentMetadata,
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
ForceRefreshQuery = Annotated[bool, Query()]
AsOfQuery = Annotated[date | None, Query()]
DividendPeriodQuery = Annotated[DividendPeriod, Query()]
IncludeDetailsQuery = Annotated[bool, Query()]
IncludeDividendsQuery = Annotated[bool, Query()]
TickersQuery = Annotated[str, Query(description="Comma-separated tickers, e.g. WEGE3,ITUB4")]
CacheTokenHeader = Annotated[str | None, Header(alias="X-Cache-Token")]


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
) -> AssetResponse:
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
) -> AssetDetails:
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
) -> list[Dividend]:
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
) -> BatchAssetResponse:
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
    settings = get_settings()
    configured = (
        settings.cache_invalidate_token.get_secret_value()
        if settings.cache_invalidate_token
        else None
    )
    provided = x_cache_token or payload.token
    if configured and provided != configured:
        raise UnauthorizedCacheInvalidationError()

    if payload.ticker:
        ticker = service.normalize_ticker(payload.ticker)
        await service.cache.invalidate(f"details:{ticker}")
        await service.cache.invalidate(f"dividends:{ticker}")
        return CacheInvalidationResponse(invalidated=True, ticker=ticker)

    await service.cache.invalidate()
    return CacheInvalidationResponse(invalidated=True, ticker=None)
