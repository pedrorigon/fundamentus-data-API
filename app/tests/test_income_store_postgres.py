from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.core.postgres import (
    normalize_database_url,
    postgres_connect,
    postgres_row_factory,
)
from app.income import store as income_store
from app.income.store import IncomeEventStore
from app.models.income_events import (
    CanonicalIncomeEvent,
    IncomeEventObservation,
    IncomeEventStatus,
    IncomeSourceCoverage,
)

PG_URL = "postgresql+asyncpg://user:pass@db/test"


@dataclass
class _Result:
    rows: list[dict[str, Any]] | None = None
    row: dict[str, Any] | None = None
    rowcount: int = 0


@dataclass
class _FakeDatabase:
    responses: list[_Result] = field(default_factory=list)
    statements: list[tuple[str, Any]] = field(default_factory=list)
    connections: list[_FakeConnection] = field(default_factory=list)


class _FakeCursor:
    def __init__(self, database: _FakeDatabase) -> None:
        self.database = database
        self.rowcount = 0
        self._rows: list[dict[str, Any]] = []
        self._row: dict[str, Any] | None = None

    async def execute(self, query: str, params: Any = ()) -> None:
        self.database.statements.append((query, params))
        result = self.database.responses.pop(0) if self.database.responses else _Result()
        self.rowcount = result.rowcount
        self._rows = list(result.rows or [])
        self._row = result.row

    async def executemany(self, query: str, params_seq: Any) -> None:
        self.database.statements.append((query, list(params_seq)))

    async def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)

    async def fetchone(self) -> dict[str, Any] | None:
        return self._row


class _FakeConnection:
    def __init__(self, database: _FakeDatabase) -> None:
        self.database = database
        self.closed = False
        self.transactions = 0

    def cursor(self, *, row_factory: object | None = None) -> _FakeCursor:
        del row_factory
        return _FakeCursor(self.database)

    def transaction(self) -> _FakeTransaction:
        self.transactions += 1
        return _FakeTransaction()

    async def execute(self, query: str, params: Any = ()) -> None:
        self.database.statements.append((query, params))

    async def close(self) -> None:
        self.closed = True


class _FakeTransaction:
    async def __aenter__(self) -> _FakeTransaction:
        return self

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        return None


async def _store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *responses: _Result,
) -> tuple[IncomeEventStore, _FakeDatabase]:
    database = _FakeDatabase(responses=list(responses))

    async def connect(_database_url: str | None) -> _FakeConnection:
        connection = _FakeConnection(database)
        database.connections.append(connection)
        return connection

    monkeypatch.setattr(income_store, "postgres_connect", connect)
    store = IncomeEventStore(tmp_path / "income.sqlite3", database_url=PG_URL)
    return store, database


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


def _coverage(ticker: str = "BBAS3") -> IncomeSourceCoverage:
    return IncomeSourceCoverage(source="b3", ticker=ticker, status="complete", complete=True)


def test_database_url_normalization_accepts_driver_prefixes() -> None:
    assert normalize_database_url("") is None
    assert normalize_database_url("postgresql+asyncpg://db/app") == "postgresql://db/app"
    assert normalize_database_url("postgres+psycopg://db/app") == "postgresql://db/app"
    assert normalize_database_url("postgresql://db/app") == "postgresql://db/app"


def test_postgres_row_factory_returns_a_row_callable() -> None:
    cursor = type("_Cursor", (), {"pgresult": None})()
    assert callable(postgres_row_factory(cursor))


async def test_postgres_connect_opens_a_psycopg_async_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class _AsyncConnection:
        @staticmethod
        async def connect(database_url: str) -> object:
            calls.append(database_url)
            return object()

    fake = type("_Psycopg", (), {"AsyncConnection": _AsyncConnection})
    monkeypatch.setitem(sys.modules, "psycopg", fake())

    connection = await postgres_connect("postgresql://db/app")

    assert connection is not None
    assert calls == ["postgresql://db/app"]


async def test_postgres_startup_creates_the_shared_schema(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, database = await _store(monkeypatch, tmp_path)

    assert store.postgres is True
    assert store.database_url == "postgresql://user:pass@db/test"
    await store.startup()
    await store.startup()

    statements = [query for query, _params in database.statements]
    assert any("CREATE TABLE IF NOT EXISTS canonical_income_events" in q for q in statements)
    assert any("CREATE TABLE IF NOT EXISTS income_refresh_job_items" in q for q in statements)
    partial = next(q for q in statements if "uq_income_refresh_item_inflight" in q)
    assert "WHERE status IN ('queued', 'running')" in partial
    sequence = next(q for q in statements if "INSERT INTO income_event_sequence" in q)
    assert "ON CONFLICT (singleton) DO NOTHING" in sequence
    assert len(database.connections) == 1
    assert database.connections[0].closed is True
    await store.close()


async def test_postgres_observations_round_trip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observation = _observation()
    payload = json.dumps(observation.model_dump(mode="json"))
    store, database = await _store(
        monkeypatch,
        tmp_path,
        _Result(rows=[{"payload": payload}]),
        _Result(rowcount=1),
    )

    await store.save_observations([observation])
    rows = await store.observations(["bbas3"])

    assert [row.ticker for row in rows] == ["BBAS3"]
    insert_query, insert_params = database.statements[0]
    assert "%s" in insert_query
    assert "?" not in insert_query
    assert insert_params[0][3] == "BBAS3"


async def test_postgres_replace_observations_retires_the_overlap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, database = await _store(monkeypatch, tmp_path, _Result(rowcount=0))

    replaced = await store.replace_observations(
        [],
        snapshot_sources=("b3",),
        complete_tickers=["bbas3"],
        snapshot_from=date(2026, 1, 1),
    )

    assert replaced == 0
    query, params = database.statements[0]
    assert "SET active = 0" in query
    assert list(params) == ["b3", "BBAS3", "2026-01-01"]


async def test_postgres_publish_skips_semantic_duplicates_and_bumps_revision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    existing = _event()
    stored = existing.model_copy(update={"revision": 3})
    store, database = await _store(
        monkeypatch,
        tmp_path,
        _Result(row={"payload": json.dumps(stored.model_dump(mode="json"))}),
    )

    changed = await store.publish([existing])

    assert changed == 0
    assert len(database.statements) == 1


async def test_postgres_publish_writes_a_new_revision_with_the_sequence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, database = await _store(
        monkeypatch,
        tmp_path,
        _Result(row=None),
        _Result(rowcount=1),
        _Result(row={"value": 7}),
        _Result(rowcount=1),
    )

    changed = await store.publish([_event()])

    assert changed == 1
    queries = [query for query, _params in database.statements]
    assert "UPDATE income_event_sequence" in queries[1]
    assert "ON CONFLICT(event_id) DO UPDATE" in queries[3]
    insert_params = database.statements[3][1]
    assert insert_params[5] == 1
    assert insert_params[7] == 7


async def test_postgres_publish_cancels_events_missing_from_the_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stale = _event("income:stale")
    store, database = await _store(
        monkeypatch,
        tmp_path,
        _Result(row=None),
        _Result(rowcount=1),
        _Result(row={"value": 1}),
        _Result(rowcount=1),
        _Result(rows=[{"payload": json.dumps(stale.model_dump(mode="json"))}]),
        _Result(rowcount=1),
        _Result(row={"value": 2}),
        _Result(rowcount=1),
    )

    changed = await store.publish([_event()], scope_tickers=["BBAS3"])

    assert changed == 2
    update = next(q for q, _p in database.statements if "SET status =" in q)
    assert "canonical_income_events" in update


async def test_postgres_reads_map_rows_and_filters(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    event = _event()
    event_payload = json.dumps(event.model_dump(mode="json"))
    coverage_payload = json.dumps(_coverage().model_dump(mode="json"))
    store, database = await _store(
        monkeypatch,
        tmp_path,
        _Result(rows=[{"payload": event_payload}]),
        _Result(rows=[{"payload": coverage_payload}]),
        _Result(rows=[{"payload": event_payload, "changed_seq": 4}]),
        _Result(row={"value": 4}),
        _Result(row={"total": 2}),
        _Result(rows=[{"ticker": "BBAS3"}]),
    )

    events = await store.events(["BBAS3"], from_date=date(2026, 1, 1), include_tentative=True)
    coverage = await store.coverage(["bbas3"])
    changes, next_cursor, has_more = await store.changes(1, limit=50)
    cursor = await store.cursor()
    empty = await store.fresh_coverage("b3", [], not_before=datetime.now(UTC))
    pending = await store.pending_refresh_item_count("job-1")
    tickers = await store.job_tickers("job-1")

    assert [item.event_id for item in events] == ["income:1"]
    assert [item.ticker for item in coverage] == ["BBAS3"]
    assert [(item.event_id, next_cursor, has_more) for item in changes] == [("income:1", 4, False)]
    assert cursor == 4
    assert empty == set()
    assert pending == 2
    assert tickers == ["BBAS3"]
    events_query = database.statements[0][0]
    assert "payment_date >= %s" in events_query
    assert "status IN (%s,%s,%s)" in events_query


async def test_postgres_claims_items_with_row_locks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, database = await _store(
        monkeypatch,
        tmp_path,
        _Result(rowcount=1),
        _Result(
            rows=[
                {
                    "job_id": "job-1",
                    "source": "official_companies",
                    "ticker": "BBAS3",
                    "attempts": 1,
                    "as_of": "2026-09-18",
                }
            ]
        ),
    )
    now = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)

    claimed = await store.claim_refresh_items(limit=32, lease_seconds=120, now=now)

    assert claimed == [
        {
            "job_id": "job-1",
            "source": "official_companies",
            "ticker": "BBAS3",
            "attempts": 1,
            "as_of": "2026-09-18",
        }
    ]
    select_query = database.statements[1][0]
    assert "ORDER BY i.id" in select_query
    assert "FOR UPDATE OF i SKIP LOCKED" in select_query
    assert "%s" in select_query
    assert len(database.statements) == 4


async def test_postgres_job_lifecycle_counts_only_new_items(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, database = await _store(
        monkeypatch,
        tmp_path,
        _Result(rowcount=0),
        _Result(rowcount=1),
        _Result(rowcount=0),
        _Result(
            row={
                "job_id": "job-1",
                "status": "running",
                "requested": 2,
                "error": None,
                "created_at": "2026-09-18T12:00:00+00:00",
                "updated_at": "2026-09-18T12:00:00+00:00",
            }
        ),
        _Result(
            rows=[
                {
                    "source": "b3",
                    "ticker": "BBAS3",
                    "status": "complete",
                    "attempts": 1,
                    "last_error": None,
                },
                {
                    "source": "b3",
                    "ticker": "PETR4",
                    "status": "failed",
                    "attempts": 3,
                    "last_error": "boom",
                },
            ]
        ),
    )
    now = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)

    inserted = await store.create_refresh_job(
        "job-1",
        [("b3", "bbas3"), ("b3", "petr4")],
        requested=2,
        as_of=date(2026, 9, 18),
        now=now,
    )
    job = await store.refresh_job("job-1")
    await store.complete_refresh_item("job-1", "b3", "bbas3", now=now)
    await store.requeue_refresh_item(
        "job-1",
        "b3",
        "petr4",
        error="boom",
        available_at=now,
        now=now,
    )
    await store.fail_refresh_item("job-1", "b3", "petr4", error="boom", now=now)
    await store.finish_refresh_job("job-1", status="partial", error=None, now=now)

    assert inserted == 1
    assert job is not None
    assert (job["completed"], job["failed"]) == (1, 1)
    item_insert = database.statements[1][0]
    assert "ON CONFLICT DO NOTHING" in item_insert
    assert "%s" in item_insert
    assert database.statements[-1][1][0] == "partial"


async def test_postgres_sessions_report_missing_sequence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, _database = await _store(
        monkeypatch,
        tmp_path,
        _Result(row=None),
        _Result(rowcount=1),
        _Result(row=None),
    )

    with pytest.raises(RuntimeError, match="sequence is unavailable"):
        await store.publish([_event()])
