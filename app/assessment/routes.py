from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request, Response, status

from app.assessment.models import (
    AssessmentRunStatus,
    AssessmentSnapshotRequest,
    AssessmentSnapshotResponse,
)
from app.assessment.service import AssessmentSnapshotService

router = APIRouter()


def get_assessment_service(request: Request) -> AssessmentSnapshotService:
    return cast(AssessmentSnapshotService, request.app.state.assessment_service)


AssessmentServiceDep = Annotated[AssessmentSnapshotService, Depends(get_assessment_service)]


@router.post(
    "/v1/assessments/snapshot",
    response_model=AssessmentSnapshotResponse,
    tags=["assessments"],
)
async def resolve_assessment_snapshot(
    payload: AssessmentSnapshotRequest,
    service: AssessmentServiceDep,
    response: Response,
) -> AssessmentSnapshotResponse:
    result = await service.resolve(payload)
    if result.status is AssessmentRunStatus.processing:
        response.status_code = status.HTTP_202_ACCEPTED
    elif result.status is AssessmentRunStatus.failed:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    if result.retry_after_seconds is not None:
        # Do not let a malformed/stale persisted value produce an unbounded
        # HTTP header or force clients into an impractical polling interval.
        response.headers["Retry-After"] = str(min(3600, max(1, result.retry_after_seconds)))
    return result


__all__ = ["get_assessment_service", "router"]
