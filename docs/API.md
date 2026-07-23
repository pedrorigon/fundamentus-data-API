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

### `POST /v1/equities/historical-quotes/resolve`

Resolves public B3 COTAHIST closing prices for Brazilian equities on or before
each requested date. The response preserves the requested date, the effective
trading date and unavailable tickers. COTAHIST prices are historical trade
prices and are not adjusted for corporate actions.

### `GET /v2/instruments/{ticker}`

Returns normalized data for domestic and international stocks and ETFs.

Query parameters:

- `instrument_type`: optional `stock` or `etf` hint for international symbols

Domestic instruments are classified from B3 files and enriched with brapi quotes. International ETFs use the Alpha Vantage ETF profile, including holdings and allocations. International stocks use the Alpha Vantage company overview. Provider failures leave the affected optional section empty without changing the response schema.

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
# Fixed-income valuations

`POST /v1/fixed-income/valuations/resolve` resolves public indicative unit prices for a
bounded list of instrument identifiers and valuation dates. The service currently uses the
official ANBIMA daily debenture publication, caches immutable historical files, and returns
unavailable identifiers explicitly instead of inventing a price.

```json
{
  "identifiers": ["AALM12", "CDB925623O7"],
  "dates": ["2026-07-17"]
}
```

The response preserves an empty list for instruments that no public source can value, allowing
callers to retain the instrument and its transactions while calculating performance from the
measurable part of a portfolio.
