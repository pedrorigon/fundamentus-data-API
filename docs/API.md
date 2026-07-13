# API Reference

Base URL: `http://127.0.0.1:8000`

OpenAPI: `/openapi.json`

Swagger UI: `/docs`

## Endpoints

### `GET /health`

Returns process status and basic runtime configuration.

### `GET /metrics`

Returns Prometheus-compatible metrics.

### `GET /v1/assets/{ticker}`

Returns asset details and dividends in one request.

Query parameters:

- `include_details`: default `true`
- `include_dividends`: default `true`
- `period`: `all`, `past`, `future`, `upcoming_ex_date`
- `as_of`: `YYYY-MM-DD`
- `force_refresh`: default `false`

### `GET /v1/assets/{ticker}/details`

Returns normalized fields and all parsed sections from the Fundamentus details page.

### `GET /v1/assets/{ticker}/dividends`

Returns all parsed dividends after applying the requested period filter.

### `GET /v1/assets/{ticker}/opportunity`

Returns current price, P/L, P/VP, trailing dividend yield, Graham price,
Bazin price and the 52-week range. Each metric includes its source,
reference date and an explicit reason when unavailable. Fundamentus is the
primary source and Status Invest fills market-sensitive gaps.

### `GET /v1/instruments/{ticker}`

Classifies the ticker using official B3 instrument data. The response includes
the normalized instrument type, confidence, source and reference date. This
also covers listed instruments that Fundamentus does not expose, such as
FI-Infra and Fiagro funds.

### `GET /v1/assets`

Batch endpoint.

Query parameters:

- `tickers`: comma-separated ticker list
- `include_details`: default `true`
- `include_dividends`: default `false`
- `period`: `all`, `past`, `future`, `upcoming_ex_date`
- `as_of`: `YYYY-MM-DD`
- `force_refresh`: default `false`

### `POST /v1/cache/invalidate`

Invalidates a single ticker cache entry or the full cache.

Request body:

```json
{
  "ticker": "ITUB4",
  "token": "optional-token"
}
```

If `FUNDAMENTUS_API_CACHE_INVALIDATE_TOKEN` is set, callers must provide the token either in `X-Cache-Token` or in the request body.

## Error Shape

```json
{
  "error": {
    "code": "UPSTREAM_UNAVAILABLE",
    "message": "Fundamentus is unavailable.",
    "ticker": "WEGE3",
    "retryable": true
  }
}
```
