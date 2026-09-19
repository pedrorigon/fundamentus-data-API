"""Opt-in integration tests against a real PostgreSQL database.

Set ``FUNDAMENTUS_TEST_POSTGRES_URL`` to a scratch database URL to run them;
they drop and recreate the income tables, so never point them at production.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.postgres import postgres_connect
from app.income.store import REFRESH_COMPLETED, IncomeEventStore
from app.models.income_events import (
    CanonicalIncomeEvent,
    IncomeEventObservation,
    IncomeEventStatus,
    IncomeSourceCoverage,
)

DSN = os.environ.get("FUNDAMENTUS_TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not DSN,
    reason="FUNDAMENTUS_TEST_POSTGRES_URL is not configured",
)

_TABLES = (
    "income_refresh_job_items",
    "income_refresh_jobs",
    "income_source_coverage",
    "canonical_income_events",
    "income_event_observations",
    "income_event_sequence",
)


async def _reset() -> None:
    connection = await postgres_connect(DSN)
    try:
        for table in _TABLES:
            await connection.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        await connection.commit()
    finally:
        await connection.close()


async def _store() -> IncomeEventStore:
    store = IncomeEventStore(Path("unused.sqlite3"), database_url=DSN)
    await store.startup()
    return store


def _observation(ticker: str = "BBAS3") -> IncomeEventObservation:
    return IncomeEventObservation(
        source="b3",
        lineage="official:b3",
        source_event_id=f"{ticker}-1",
        ticker=ticker,
        event_type="dividend",
        payment_date=date(2026, 9, 1),
        unit_price=Decimal("0.50"),
        authority=90,
        payload_hash="hash",
    )


def _event(event_id: str = "income:1", *, ticker: str = "BBAS3") -> CanonicalIncomeEvent:
    return CanonicalIncomeEvent(
        event_id=event_id,
        ticker=ticker,
        event_type="dividend",
        ex_date=date(2026, 8, 20),
        payment_date=date(2026, 9, 1),
        unit_price=Decimal("0.50"),
        status=IncomeEventStatus.corroborated,
    )


async def _run_lifecycle() -> None:
    first = await _store()
    second = await _store()
    try:
        assert await first.save_observations([_observation()]) == 1
        stored = await second.observations(["bbas3"])
        assert [(item.ticker, item.source) for item in stored] == [("BBAS3", "b3")]

        await first.save_coverage(
            [IncomeSourceCoverage(source="b3", ticker="BBAS3", status="ok", complete=True)]
        )
        fresh = await second.fresh_coverage(
            "b3",
            ["BBAS3"],
            not_before=datetime.now(UTC) - timedelta(seconds=60),
        )
        assert fresh == {"BBAS3"}

        assert await first.publish([_event()], scope_tickers=["BBAS3"]) == 1
        assert await second.cursor() == 1
        events = await second.events(["BBAS3"])
        assert [item.event_id for item in events] == ["income:1"]
        changes, next_cursor, has_more = await second.changes(0, limit=10)
        assert ([item.event_id for item in changes], next_cursor, has_more) == (
            ["income:1"],
            1,
            False,
        )

        assert await first.publish([], scope_tickers=["BBAS3"]) == 1
        cancelled, cancelled_cursor, _ = await second.changes(1, limit=10)
        assert [item.status for item in cancelled] == [IncomeEventStatus.cancelled]
        assert cancelled_cursor == 2

        now = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
        inserted = await first.create_refresh_job(
            "job-1",
            [("official_companies", "BBAS3")],
            requested=1,
            as_of=date(2026, 9, 18),
            now=now,
        )
        assert inserted == 1
        single_flight = await second.create_refresh_job(
            "job-2",
            [("official_companies", "BBAS3")],
            requested=1,
            as_of=date(2026, 9, 18),
            now=now,
        )
        assert single_flight == 0
        claimed = await first.claim_refresh_items(limit=10, lease_seconds=120, now=now)
        assert [(item["job_id"], item["source"], item["ticker"]) for item in claimed] == [
            ("job-1", "official_companies", "BBAS3")
        ]
        assert await second.claim_refresh_items(limit=10, lease_seconds=120, now=now) == []
        await first.complete_refresh_item(
            "job-1",
            "official_companies",
            "BBAS3",
            now=now,
        )
        assert await second.pending_refresh_item_count("job-1") == 0
        assert await second.job_tickers("job-1") == ["BBAS3"]
        job = await second.refresh_job("job-1")
        assert job is not None and job["completed"] == 1
        await first.finish_refresh_job("job-1", status=REFRESH_COMPLETED, error=None, now=now)
        assert (await second.refresh_job("job-1") or {})["status"] == REFRESH_COMPLETED

        reinserted = await second.create_refresh_job(
            "job-3",
            [("official_companies", "BBAS3")],
            requested=1,
            as_of=date(2026, 9, 18),
            now=now,
        )
        assert reinserted == 1
        claim_a, claim_b = await asyncio.gather(
            first.claim_refresh_items(limit=1, lease_seconds=120, now=now),
            second.claim_refresh_items(limit=1, lease_seconds=120, now=now),
        )
        owners = [(item["job_id"], item["ticker"]) for item in [*claim_a, *claim_b]]
        assert owners == [("job-3", "BBAS3")]

        await second.requeue_refresh_item(
            "job-3",
            "official_companies",
            "BBAS3",
            error="boom",
            available_at=now,
            now=now,
        )
        retried = await first.claim_refresh_items(limit=1, lease_seconds=120, now=now)
        assert [(item["job_id"], item["attempts"]) for item in retried] == [("job-3", 2)]
        await second.fail_refresh_item(
            "job-3",
            "official_companies",
            "BBAS3",
            error="boom",
            now=now,
        )
        assert (await first.refresh_job("job-3") or {})["failed"] == 1
    finally:
        await first.close()
        await second.close()


def test_postgres_store_lifecycle_is_shared_across_instances() -> None:
    asyncio.run(_reset())
    asyncio.run(_run_lifecycle())
