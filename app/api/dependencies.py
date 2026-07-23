from typing import cast

from fastapi import Request

from app.services import (
    AssetService,
    FixedIncomeValuationService,
    HistoricalQuoteService,
    InstrumentDataService,
    OpportunityService,
)


def get_asset_service(request: Request) -> AssetService:
    return cast(AssetService, request.app.state.asset_service)


def get_opportunity_service(request: Request) -> OpportunityService:
    return cast(OpportunityService, request.app.state.opportunity_service)


def get_instrument_data_service(request: Request) -> InstrumentDataService:
    return cast(InstrumentDataService, request.app.state.instrument_data_service)


def get_fixed_income_valuation_service(request: Request) -> FixedIncomeValuationService:
    return cast(FixedIncomeValuationService, request.app.state.fixed_income_valuation_service)


def get_historical_quote_service(request: Request) -> HistoricalQuoteService:
    return cast(HistoricalQuoteService, request.app.state.historical_quote_service)
