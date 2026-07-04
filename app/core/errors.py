from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class APIError(Exception):
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    code = "INTERNAL_ERROR"
    message = "Unexpected internal failure."
    retryable = False

    def __init__(
        self,
        message: str | None = None,
        *,
        ticker: str | None = None,
        retryable: bool | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.message
        self.ticker = ticker
        self.retryable = self.retryable if retryable is None else retryable
        self.details = details or {}
        super().__init__(self.message)

    def payload(self) -> dict[str, Any]:
        error: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.ticker:
            error["ticker"] = self.ticker
        if self.details:
            error["details"] = self.details
        return {"error": error}


class InvalidTickerError(APIError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "INVALID_TICKER"
    message = "Invalid ticker."


class AssetNotFoundError(APIError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "ASSET_NOT_FOUND"
    message = "Asset not found on Fundamentus."


class LocalRateLimitError(APIError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "LOCAL_RATE_LIMIT"
    message = "Local request limit exceeded."
    retryable = True


class UpstreamInvalidResponseError(APIError):
    status_code = status.HTTP_502_BAD_GATEWAY
    code = "UPSTREAM_INVALID_RESPONSE"
    message = "Invalid or unexpected Fundamentus response."
    retryable = True


class UpstreamUnavailableError(APIError):
    status_code = status.HTTP_502_BAD_GATEWAY
    code = "UPSTREAM_UNAVAILABLE"
    message = "Fundamentus is unavailable."
    retryable = True


class CircuitBreakerOpenError(APIError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "UPSTREAM_CIRCUIT_OPEN"
    message = "Fundamentus circuit breaker is open."
    retryable = True


class UnauthorizedCacheInvalidationError(APIError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "CACHE_INVALIDATION_UNAUTHORIZED"
    message = "Invalid cache invalidation token."


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(APIError)
    async def api_error_handler(_request: Request, exc: APIError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.payload())

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Invalid request parameters.",
                    "retryable": False,
                    "details": exc.errors(),
                }
            },
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(_request: Request, _exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "Unexpected internal failure.",
                    "retryable": False,
                }
            },
        )
