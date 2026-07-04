from typing import cast

from fastapi import Request

from app.services import AssetService


def get_asset_service(request: Request) -> AssetService:
    return cast(AssetService, request.app.state.asset_service)
