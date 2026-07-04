from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from typing import Any

import httpx


async def timed_get(
    client: httpx.AsyncClient,
    path: str,
    params: dict[str, Any] | None = None,
) -> tuple[float, int]:
    started = time.perf_counter()
    response = await client.get(path, params=params)
    elapsed_ms = (time.perf_counter() - started) * 1000
    return elapsed_ms, response.status_code


async def run(base_url: str, ticker: str, hot_runs: int) -> None:
    async with httpx.AsyncClient(base_url=base_url, timeout=60) as client:
        cold_ms, cold_status = await timed_get(
            client,
            f"/v1/assets/{ticker}",
            {"force_refresh": "true", "period": "all"},
        )
        hot_results = [
            await timed_get(client, f"/v1/assets/{ticker}", {"period": "all"})
            for _ in range(hot_runs)
        ]

    hot_times = [elapsed for elapsed, _status in hot_results]
    print(f"ticker={ticker}")
    print(f"cold_ms={cold_ms:.2f} status={cold_status}")
    print(
        "hot_ms="
        f"min:{min(hot_times):.2f} "
        f"median:{statistics.median(hot_times):.2f} "
        f"max:{max(hot_times):.2f} "
        f"runs:{hot_runs}"
    )
    print("hot_statuses=" + ",".join(str(status) for _elapsed, status in hot_results))


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark cold and hot local API responses.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--ticker", default="ITUB4")
    parser.add_argument("--hot-runs", type=int, default=10)
    args = parser.parse_args()
    asyncio.run(run(args.base_url, args.ticker.upper(), args.hot_runs))


if __name__ == "__main__":
    main()
