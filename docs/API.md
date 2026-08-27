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

### `POST /v2/income-events/refresh`

Refreshes a bounded list of instruments from independent public sources and publishes a canonical event revision. This maintenance endpoint requires `X-Cache-Token` outside local/test environments. It is intended for background jobs, not request-time reads.

```json
{
  "instruments": [
    {"ticker": "BBAS3", "isin": "BRBBASACNOR3", "name": "Banco do Brasil"},
    {"ticker": "HGLG11", "isin": "BRHGLGCTF004", "name": "CSHG Logística"}
  ],
  "as_of": "2026-08-27"
}
```

### `POST /v2/income-events/batch`

Reads canonical events for at most 20 tickers from local SQLite storage. By default, only `corroborated` and `verified` events are returned; tentative and conflicting observations cannot inflate portfolio projections. Optional `from_date` and `to_date` fields filter payment dates. The response includes a cursor and ETag.

### `GET /v2/income-events/changes`

Returns semantic event changes after a monotonic `cursor`, with a bounded `limit` of 1 to 500. Consumers use this endpoint for incremental synchronization without repeatedly transferring the full event catalog.

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

### `POST /v1/quality/facts:resolve`

Resolves a maximum of 20 assets in one bounded request. Supported kinds are
`stock`, `real_estate_fund`, `etf`, `crypto` and `fixed_income`.

Stocks receive normalized profitability, cash conversion, growth, solvency,
liquidity and dilution evidence from CVM statements. Listed funds receive
reporting and distribution-history evidence. ETFs receive cost, scale, age and
diversification evidence when a public fund profile is available.

Every fact includes its unit, reference date, source, confidence and explicit
availability status. Cryptocurrency and fixed-income requests remain in the
same batch response but report the specialized source or identifier that is
still required, rather than inferring quality from a ticker.

```json
{
  "assets": [
    {"ticker": "WEGE3", "kind": "stock"},
    {"ticker": "HGLG11", "kind": "real_estate_fund"},
    {"ticker": "VOO", "kind": "etf"}
  ]
}
```

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
bounded list of instrument identifiers and valuation dates. The service uses the following
free sources, in order:

- ANBIMA's official daily debenture publication;
- the public ANBIMA Data CRI/CRA table (the last five business days);
- B3's public BDI consolidated fixed-income trades (from the BDI history window).

All observations are cached with their source and reference date. These feeds only provide a
market PU when the instrument was priced or traded. They do not contain the contractual terms
needed to accrue an untraded LCI, LCA, CDB or private note. Such instruments remain explicitly
unavailable instead of receiving a synthetic price.

```json
{
  "identifiers": ["AALM12", "CDB925623O7"],
  "dates": ["2026-07-17"]
}
```

The response preserves an empty list for instruments that no public source can value, allowing
callers to retain the instrument and its transactions while calculating performance from the
measurable part of a portfolio. `unavailable_reasons` explains that a contractual accrual
requires issuer, indexer/rate, acquisition and maturity terms; the service never substitutes a
zero or an estimated price.
