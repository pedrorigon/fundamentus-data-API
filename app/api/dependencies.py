from typing import cast

from fastapi import Request

from app.services import AssetService, OpportunityService


def get_asset_service(request: Request) -> AssetService:
    return cast(AssetService, request.app.state.asset_service)


def get_opportunity_service(request: Request) -> OpportunityService:
    return cast(OpportunityService, request.app.state.opportunity_service)
