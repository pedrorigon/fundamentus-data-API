# Fundamentus Data API

[![CI](https://github.com/your-org/fundamentus-data-api/actions/workflows/ci.yml/badge.svg)](https://github.com/your-org/fundamentus-data-api/actions/workflows/ci.yml)

Local HTTP API for Fundamentus asset details and dividend events. It uses direct public HTML requests, FastAPI, Pydantic v2, async `httpx`, `selectolax`, in-memory cache and optional SQLite persistence.

The service is designed for local consumption by portfolio tools, data pipelines and research scripts. It binds to `127.0.0.1` by default and does not use Selenium, Playwright or a headless browser.

## Features

- FastAPI with OpenAPI at `/openapi.json` and Swagger UI at `/docs`
- Async HTTP client with keep-alive pooling, timeouts, retries and concurrency limits
- High-performance HTML parsing with `selectolax`
- Brazilian dates, numbers, percentages and monetary values normalized without floats
- Raw values preserved beside normalized values
- Full section preservation for stocks, banks, FIIs, BDRs and other asset classes
- Dividend filters for `all`, `past`, `future` and `upcoming_ex_date`
- In-memory cache with optional SQLite persistence
- Single-flight request coalescing for concurrent cache misses
- Prometheus-compatible metrics at `/metrics`
- Consistent JSON error contract
- Fixture-based test suite that does not require internet access

## Installation

Python 3.12+ is required.

```bash
uv sync --python 3.12 --extra dev
cp .env.example .env
```

## Run

```bash
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open:

- Swagger UI: `http://127.0.0.1:8000/docs`
- Health: `http://127.0.0.1:8000/health`
- Metrics: `http://127.0.0.1:8000/metrics`

## Docker

```bash
docker compose up --build
```

The compose file publishes the service only on `127.0.0.1:8000`.

## Examples

Combined asset response:

```bash
curl 'http://127.0.0.1:8000/v1/assets/ITUB4?period=all'
```

Details only:

```bash
curl 'http://127.0.0.1:8000/v1/assets/WEGE3/details'
```

Future dividend payments:

```bash
curl 'http://127.0.0.1:8000/v1/assets/ITUB4/dividends?period=future&as_of=2026-07-04'
```

Upcoming ex-date events:

```bash
curl 'http://127.0.0.1:8000/v1/assets/ITUB4/dividends?period=upcoming_ex_date'
```

Batch query:

```bash
curl 'http://127.0.0.1:8000/v1/assets?tickers=WEGE3,ITUB4&include_dividends=false'
```

Force refresh:

```bash
curl 'http://127.0.0.1:8000/v1/assets/ITUB4?force_refresh=true'
```

Cache invalidation:

```bash
curl -X POST 'http://127.0.0.1:8000/v1/cache/invalidate' \
  -H 'Content-Type: application/json' \
  -d '{"ticker":"ITUB4"}'
```

With token protection:

```bash
curl -X POST 'http://127.0.0.1:8000/v1/cache/invalidate' \
  -H 'Content-Type: application/json' \
  -H 'X-Cache-Token: your-token' \
  -d '{"ticker":"ITUB4"}'
```

## Configuration

Every setting uses the `FUNDAMENTUS_API_` prefix. See `.env.example`.

Important settings:

- `BIND_HOST`: default `127.0.0.1`
- `BIND_PORT`: default `8000`
- `MARKET_DATA_TTL_SECONDS`: short market data TTL
- `FUNDAMENTALS_TTL_SECONDS`: fundamentals TTL
- `DIVIDENDS_TTL_SECONDS`: dividend TTL
- `SQLITE_CACHE_ENABLED`: enables persistent cache
- `BATCH_LIMIT`: maximum tickers accepted by `/v1/assets`
- `UPSTREAM_CONCURRENCY`: maximum concurrent Fundamentus requests
- `UPSTREAM_MIN_INTERVAL_SECONDS`: minimum interval between upstream requests
- `CACHE_INVALIDATE_TOKEN`: optional token for cache invalidation

Fundamentus serves market data and fundamentals in the same details page. The API uses the lower value between `MARKET_DATA_TTL_SECONDS` and `FUNDAMENTALS_TTL_SECONDS` for that full document.

## Data Shape

Each preserved details field includes normalized and raw data:

```json
{
  "label": "Valor de mercado",
  "key_normalized": "valor_de_mercado",
  "value": "471288000000",
  "raw_value": "471.288.000.000",
  "value_type": "money"
}
```

Null-like visual values such as empty text, `-` and unavailable markers are returned as `null`.

Dividend event:

```json
{
  "ex_date": "2026-06-19",
  "payment_date": "2027-03-10",
  "value": "0.1044",
  "type": "JRS CAP PROPRIO",
  "is_future_payment": true,
  "is_future_ex_date": false,
  "raw": {
    "date": "19/06/2026",
    "value": "0,1044",
    "payment_date": "10/03/2027",
    "type": "JRS CAP PROPRIO",
    "shares_ratio": "1"
  }
}
```

## Development

```bash
make install
make check
```

Equivalent commands:

```bash
uv run ruff format .
uv run ruff check .
uv run mypy app
uv run pytest
```

## Benchmark

Run the API first, then:

```bash
uv run python scripts/benchmark.py --ticker ITUB4 --hot-runs 10
```

The script performs one cold request with `force_refresh=true` and then measures hot cached responses.

## Documentation

- [API reference](docs/API.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Changelog](CHANGELOG.md)

## Scraping Policy

This project does not bypass CAPTCHA, rate limits, authentication walls or other protection mechanisms. If Fundamentus becomes unavailable or returns unexpected HTML, the API fails with a structured error instead of exposing raw HTML, cookies, sensitive headers or stack traces.

## License

MIT
