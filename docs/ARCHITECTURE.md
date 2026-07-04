# Architecture

The service is split into explicit layers:

- `app/api`: HTTP routes and dependency wiring
- `app/config`: environment-driven settings
- `app/core`: errors, metrics and time helpers
- `app/models`: Pydantic contracts
- `app/scrapers`: upstream HTTP access
- `app/parsers`: HTML parsing and Brazilian value normalization
- `app/cache`: in-memory and SQLite cache
- `app/services`: application orchestration

## Request Flow

1. Routes validate query parameters and call `AssetService`.
2. `AssetService` normalizes tickers and checks cache.
3. Cache hits return parsed objects from memory or JSON payloads from SQLite.
4. Cache misses enter single-flight request coalescing.
5. The scraper fetches direct public HTML from Fundamentus through a shared `httpx.AsyncClient`.
6. Parsers run off the event loop with `asyncio.to_thread`.
7. Parsed models are cached and returned through FastAPI.

## Performance Choices

- One shared upstream HTTP client with keep-alive pooling.
- One long-lived SQLite connection with WAL mode.
- Memory cache stores parsed objects for low-latency hot reads.
- Persistent cache stores JSON-compatible payloads through `orjson`.
- Single-flight avoids duplicate scrapes for concurrent identical misses.
- Circuit breaker stops repeated upstream calls during outages.
