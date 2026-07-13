# Fundamentus Data API

[![CI](https://github.com/pedrorigon/fundamentus-data-API/actions/workflows/ci.yml/badge.svg)](https://github.com/pedrorigon/fundamentus-data-API/actions/workflows/ci.yml)
[![Release](https://github.com/pedrorigon/fundamentus-data-API/actions/workflows/release.yml/badge.svg)](https://github.com/pedrorigon/fundamentus-data-API/actions/workflows/release.yml)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Local HTTP API for Fundamentus asset details and dividend events. It uses direct public HTML requests, FastAPI, Pydantic v2, async `httpx`, `selectolax`, in-memory cache and optional SQLite persistence.

The service is designed for local consumption by portfolio tools, data pipelines and research scripts. It binds to `127.0.0.1` by default and does not use Selenium, Playwright or a headless browser.

API version is resolved from package metadata at runtime. Release artifacts use the pushed Git tag as the build version.

## What It Does

- Exposes a typed HTTP API for asset details and dividend events from Fundamentus.
- Preserves raw table values while also returning normalized Brazilian dates, numbers, percentages and monetary values.
- Supports stocks, banks, FIIs, BDRs and other asset classes by preserving all parsed detail sections.
- Filters dividends by `all`, `past`, `future` and `upcoming_ex_date`.
- Uses local caching to reduce repeated upstream requests.
- Provides OpenAPI docs, Prometheus-compatible metrics and consistent JSON errors.
- Ships fixture-based tests that do not require internet access.

## Project Status

This is an alpha release. The API is usable for local development and research workflows, but Fundamentus HTML can change without notice. Parser behavior, normalized fields and cache semantics may still evolve before a stable `1.0.0` release.

This project is not affiliated with, endorsed by or sponsored by Fundamentus. Data returned by this API is not investment advice.

## Requirements

- Python 3.12+
- `uv` for local development
- Docker, optional

## Quick Start

```bash
git clone https://github.com/pedrorigon/fundamentus-data-API.git
cd fundamentus-data-API
uv sync --python 3.12 --extra dev
cp .env.example .env
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open:

- Swagger UI: `http://127.0.0.1:8000/docs`
- ReDoc: `http://127.0.0.1:8000/redoc`
- Health: `http://127.0.0.1:8000/health`
- Metrics: `http://127.0.0.1:8000/metrics`

## Docker

```bash
docker compose up --build
```

The compose file publishes the service only on `127.0.0.1:8000` and stores the SQLite cache in a named Docker volume.

## API Overview

| Endpoint | Description |
| --- | --- |
| `GET /health` | Runtime status, version and basic configuration checks. |
| `GET /metrics` | Prometheus-compatible process and application metrics. |
| `GET /v1/assets/{ticker}` | Combined asset details and dividends. |
| `GET /v1/assets/{ticker}/details` | Details page fields and preserved sections. |
| `GET /v1/assets/{ticker}/dividends` | Dividend events with optional period filtering. |
| `GET /v1/assets/{ticker}/opportunity` | Current valuation metrics with source and reference date. |
| `GET /v1/instruments/{ticker}` | B3 instrument classification, including funds outside Fundamentus. |
| `GET /v1/assets` | Batch query for multiple tickers. |
| `POST /v1/cache/invalidate` | Invalidate one ticker or the full local cache. |

See [docs/API.md](docs/API.md) for the full endpoint reference.

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
curl 'http://127.0.0.1:8000/v1/assets/ITUB4/dividends?period=future&as_of=2026-07-05'
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

Every setting uses the `FUNDAMENTUS_API_` prefix. Start from [.env.example](.env.example).

| Setting | Default | Description |
| --- | --- | --- |
| `BIND_HOST` | `127.0.0.1` | Interface used by the local server. |
| `BIND_PORT` | `8000` | Port used by the local server. |
| `MARKET_DATA_TTL_SECONDS` | `300` | Short TTL for market-sensitive data. |
| `FUNDAMENTALS_TTL_SECONDS` | `3600` | TTL for fundamentals. |
| `DIVIDENDS_TTL_SECONDS` | `21600` | TTL for dividend events. |
| `OPPORTUNITY_CACHE_TTL_SECONDS` | `900` | In-memory TTL for B3 and Status Invest complements. |
| `SQLITE_CACHE_ENABLED` | `true` | Enables persistent local cache. |
| `SQLITE_CACHE_PATH` | `.cache/fundamentus_cache.sqlite3` | SQLite cache path. |
| `BATCH_LIMIT` | `20` | Maximum tickers accepted by `/v1/assets`. |
| `UPSTREAM_CONCURRENCY` | `4` | Maximum concurrent Fundamentus requests. |
| `UPSTREAM_MIN_INTERVAL_SECONDS` | `0.15` | Minimum interval between upstream requests. |
| `CACHE_INVALIDATE_TOKEN` | empty | Optional token for cache invalidation. |

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

Run the API locally:

```bash
make run
```

## Benchmark

Run the API first, then:

```bash
uv run python scripts/benchmark.py --ticker ITUB4 --hot-runs 10
```

The script performs one cold request with `force_refresh=true` and then measures hot cached responses.

## Versioning And Releases

Releases follow semantic versioning:

- Patch releases fix bugs without changing the public API contract.
- Minor releases add backwards-compatible endpoints, fields or configuration.
- Major releases may include breaking API, parser or cache changes.

Release notes live in [CHANGELOG.md](CHANGELOG.md). The release process is documented in [RELEASING.md](RELEASING.md).

Pushing a tag such as `vMAJOR.MINOR.PATCH` runs the release pipeline, injects that tag version into the build, creates a GitHub Release from the matching changelog section and uploads build artifacts.

## Documentation

- [API reference](docs/API.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Code of conduct](CODE_OF_CONDUCT.md)
- [Changelog](CHANGELOG.md)
- [Release process](RELEASING.md)

## Responsible Scraping

This project performs direct HTTP requests against public HTML. It does not bypass CAPTCHA, rate limits, authentication walls or other protection mechanisms.

If Fundamentus becomes unavailable or returns unexpected HTML, the API fails with a structured error instead of exposing raw HTML, cookies, sensitive headers or stack traces.

## License

MIT. See [LICENSE](LICENSE).
