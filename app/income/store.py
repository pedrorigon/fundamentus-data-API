"""Durable storage for canonical income events.

SQLite keeps the single-node deployment correct and restart-safe without extra
configuration. When a database URL is configured the same schema is created in
PostgreSQL and shared by every API replica: the monotonic sequence, the
canonical revisions, the source coverage and the refresh jobs all live in one
place, and job items are claimed with short transactions and row locks so two
replicas never process the same item.

Both backends run the same statements through :class:`_Session`, which adapts
the placeholder style and the few dialect-specific clauses. PostgreSQL opens a
short-lived connection per operation and never holds a transaction while a
source provider is awaited.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite
import orjson

from app.cache.sqlite import configure_sqlite_connection, sqlite_path_lock, sqlite_transaction
from app.core.postgres import normalize_database_url, postgres_connect, postgres_row_factory
from app.models.income_events import (
    CanonicalIncomeEvent,
    IncomeEventObservation,
    IncomeEventStatus,
    IncomeSourceCoverage,
)

REFRESH_QUEUED = "queued"
REFRESH_RUNNING = "running"
REFRESH_COMPLETED = "completed"
REFRESH_PARTIAL = "partial"
ITEM_QUEUED = "queued"
ITEM_RUNNING = "running"
ITEM_COMPLETE = "complete"
ITEM_FAILED = "failed"


def _schema_statements(*, postgres: bool) -> tuple[str, ...]:
    """Return the schema for one backend.

    Both backends share the same columns and indexes; PostgreSQL adds a
    surrogate identifier to the job items so claiming preserves insertion
    order, and uses the conflict form of the sequence seed.
    """
    job_item_identifier = "id BIGSERIAL NOT NULL UNIQUE," if postgres else ""
    sequence_seed = (
        """
        INSERT INTO income_event_sequence (singleton, value) VALUES (1, 0)
        ON CONFLICT (singleton) DO NOTHING
        """
        if postgres
        else "INSERT OR IGNORE INTO income_event_sequence (singleton, value) VALUES (1, 0)"
    )
    return (
        """
        CREATE TABLE IF NOT EXISTS income_event_observations (
            source TEXT NOT NULL,
            source_event_id TEXT NOT NULL,
            source_version INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            payment_date TEXT,
            payload TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (source, source_event_id, source_version)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_income_observation_ticker
            ON income_event_observations (ticker, observed_at)
        """,
        """
        CREATE TABLE IF NOT EXISTS canonical_income_events (
            event_id TEXT PRIMARY KEY,
            ticker TEXT NOT NULL,
            ex_date TEXT NOT NULL,
            payment_date TEXT NOT NULL,
            status TEXT NOT NULL,
            revision INTEGER NOT NULL,
            payload TEXT NOT NULL,
            changed_seq INTEGER NOT NULL
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_canonical_income_ticker_payment
            ON canonical_income_events (ticker, payment_date)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_canonical_income_changes
            ON canonical_income_events (changed_seq)
        """,
        """
        CREATE TABLE IF NOT EXISTS income_source_coverage (
            source TEXT NOT NULL,
            ticker TEXT NOT NULL,
            payload TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            PRIMARY KEY (source, ticker)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS income_event_sequence (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            value INTEGER NOT NULL
        )
        """,
        sequence_seed,
        """
        CREATE TABLE IF NOT EXISTS income_refresh_jobs (
            job_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            requested INTEGER NOT NULL,
            as_of TEXT NOT NULL,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS income_refresh_job_items (
            {job_item_identifier}
            job_id TEXT NOT NULL,
            source TEXT NOT NULL,
            ticker TEXT NOT NULL,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            lease_until TEXT,
            last_error TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (job_id, source, ticker)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_income_refresh_item_claim
            ON income_refresh_job_items (status, lease_until)
        """,
        # Persisted single-flight: one in-flight item per source and ticker,
        # shared by every API replica.
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_income_refresh_item_inflight
            ON income_refresh_job_items (source, ticker)
            WHERE status IN ('queued', 'running')
        """,
    )


# PostgreSQL keeps the timestamps as ISO-8601 text exactly like SQLite so the
# payloads and the lexical filters (payment_date, observed_at, leases) remain
# byte-for-byte compatible between the two backends.
_SQLITE_SCHEMA = _schema_statements(postgres=False)
_POSTGRES_SCHEMA = _schema_statements(postgres=True)

_OBSERVATION_UPSERT = """
INSERT INTO income_event_observations (
    source, source_event_id, source_version, ticker,
    payment_date, payload, observed_at, active
) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(source, source_event_id, source_version) DO UPDATE SET
    ticker = excluded.ticker,
    payment_date = excluded.payment_date,
    payload = excluded.payload,
    observed_at = excluded.observed_at,
    active = excluded.active
"""

_COVERAGE_UPSERT = """
INSERT INTO income_source_coverage (source, ticker, payload, observed_at)
VALUES (?, ?, ?, ?)
ON CONFLICT(source, ticker) DO UPDATE SET
    payload = excluded.payload,
    observed_at = excluded.observed_at
"""

_JOB_ITEM_INSERT = """
INSERT INTO income_refresh_job_items (
    job_id, source, ticker, status, attempts, lease_until,
    last_error, updated_at
) VALUES (?, ?, ?, ?, 0, NULL, NULL, ?)
"""


class _Session:
    """Run one store operation against SQLite or PostgreSQL."""

    def __init__(self, connection: Any, *, postgres: bool) -> None:
        self._connection = connection
        self.postgres = postgres

    def adapt(self, query: str) -> str:
        # Every statement in this module uses ``?`` placeholders and contains
        # no other question mark, so the translation is unambiguous.
        return query.replace("?", "%s") if self.postgres else query

    async def execute(self, query: str, params: Sequence[Any] = ()) -> Any:
        if self.postgres:
            cursor = self._connection.cursor(row_factory=postgres_row_factory)
            await cursor.execute(self.adapt(query), tuple(params))
            return cursor
        return await self._connection.execute(self.adapt(query), tuple(params))

    async def executemany(self, query: str, params: Sequence[Sequence[Any]]) -> None:
        if self.postgres:
            cursor = self._connection.cursor(row_factory=postgres_row_factory)
            await cursor.executemany(self.adapt(query), [tuple(row) for row in params])
            return
        await self._connection.executemany(self.adapt(query), [tuple(row) for row in params])

    async def insert_ignore(self, query: str, params: Sequence[Any]) -> int:
        """Insert one row, ignoring unique conflicts; returns the rows written."""
        if self.postgres:
            cursor = await self.execute(f"{query} ON CONFLICT DO NOTHING", params)
        else:
            cursor = await self.execute(
                query.replace("INSERT INTO", "INSERT OR IGNORE INTO", 1),
                params,
            )
        return max(int(cursor.rowcount or 0), 0)

    async def fetchall(self, query: str, params: Sequence[Any] = ()) -> list[Any]:
        cursor = await self.execute(query, params)
        return list(await cursor.fetchall())

    async def fetchone(self, query: str, params: Sequence[Any] = ()) -> Any:
        cursor = await self.execute(query, params)
        return await cursor.fetchone()


class IncomeEventStore:
    def __init__(self, path: Path, *, database_url: str | None = None) -> None:
        self.path = path
        self.database_url = normalize_database_url(database_url)
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._started = False

    @property
    def postgres(self) -> bool:
        return self.database_url is not None

    async def startup(self) -> None:
        if self._started:
            return
        if self.postgres:
            await self._postgres_setup()
            self._started = True
            return
        async with self._lock:
            if self._db is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = await aiosqlite.connect(self.path, timeout=5.0)
            db.row_factory = aiosqlite.Row
            self._db = db
            try:
                async with sqlite_path_lock(self.path):
                    await configure_sqlite_connection(db)
                    async with sqlite_transaction(db, self.path, acquire_lock=False):
                        for statement in _SQLITE_SCHEMA:
                            await db.execute(statement)
                        await self._ensure_observation_active_column()
                        await self._ensure_observation_payment_date_column()
            except BaseException:
                await db.close()
                self._db = None
                raise
            self._started = True

    async def close(self) -> None:
        async with self._lock:
            db = self._db
            if db is not None:
                async with sqlite_path_lock(self.path):
                    try:
                        await db.close()
                    finally:
                        self._db = None
            self._started = False

    async def save_observations(self, observations: list[IncomeEventObservation]) -> int:
        if not observations:
            return 0
        rows = [_observation_row(item) for item in observations]
        async with self._write_session() as session:
            await session.executemany(_OBSERVATION_UPSERT, rows)
        return len(rows)

    async def replace_observations(
        self,
        observations: list[IncomeEventObservation],
        *,
        snapshot_sources: tuple[str, ...],
        complete_tickers: list[str],
        snapshot_from: date,
    ) -> int:
        """Atomically replace complete source/ticker snapshots and retain failed ones."""
        if not snapshot_sources or not complete_tickers:
            return 0
        normalized_tickers = list(dict.fromkeys(ticker.upper() for ticker in complete_tickers))
        rows = [
            _observation_row(item)
            for item in observations
            if item.source in snapshot_sources and item.ticker.upper() in normalized_tickers
        ]
        source_placeholders = ",".join("?" for _ in snapshot_sources)
        ticker_placeholders = ",".join("?" for _ in normalized_tickers)
        deactivate = (
            "UPDATE income_event_observations SET active = 0 "
            f"WHERE source IN ({source_placeholders}) AND ticker IN ({ticker_placeholders}) "
            "AND payment_date >= ?"
        )  # noqa: S608 - placeholders are generated, never user-controlled
        async with self._write_session() as session:
            await session.execute(
                deactivate,
                [*snapshot_sources, *normalized_tickers, snapshot_from.isoformat()],
            )
            if rows:
                await session.executemany(_OBSERVATION_UPSERT, rows)
        return len(rows)

    async def observations(self, tickers: list[str]) -> list[IncomeEventObservation]:
        if not tickers:
            return []
        placeholders = ",".join("?" for _ in tickers)
        query = f"""
            SELECT payload FROM (
                SELECT payload, source, source_event_id, observed_at,
                       ROW_NUMBER() OVER (
                           PARTITION BY source, source_event_id
                           ORDER BY source_version DESC, observed_at DESC
                       ) AS version_rank
                FROM income_event_observations
                WHERE ticker IN ({placeholders}) AND active = 1
            ) latest
            WHERE version_rank = 1
            ORDER BY observed_at, source, source_event_id
        """  # noqa: S608 - placeholders are generated, never user-controlled
        async with self._read_session() as session:
            rows = await session.fetchall(query, [ticker.upper() for ticker in tickers])
        return [IncomeEventObservation.model_validate_json(row["payload"]) for row in rows]

    async def save_coverage(self, coverage: list[IncomeSourceCoverage]) -> None:
        if not coverage:
            return
        async with self._write_session() as session:
            await session.executemany(
                _COVERAGE_UPSERT,
                [
                    (
                        item.source,
                        item.ticker.upper(),
                        _dump(item),
                        item.observed_at.isoformat(),
                    )
                    for item in coverage
                ],
            )

    async def fresh_coverage(
        self,
        source: str,
        tickers: list[str],
        *,
        not_before: datetime,
    ) -> set[str]:
        if not tickers:
            return set()
        placeholders = ",".join("?" for _ in tickers)
        query = (
            "SELECT payload FROM income_source_coverage "
            f"WHERE source = ? AND ticker IN ({placeholders})"
        )  # noqa: S608 - placeholders are generated, never user-controlled
        async with self._read_session() as session:
            rows = await session.fetchall(query, [source, *(ticker.upper() for ticker in tickers)])
        coverage = [IncomeSourceCoverage.model_validate_json(row["payload"]) for row in rows]
        return {
            item.ticker for item in coverage if item.complete and item.observed_at >= not_before
        }

    async def publish(
        self,
        events: list[CanonicalIncomeEvent],
        *,
        scope_tickers: list[str] | None = None,
    ) -> int:
        if not events and not scope_tickers:
            return 0
        changed = 0
        async with self._write_session() as session:
            for event in events:
                existing = await self._existing(session, event.event_id)
                revision = existing.revision if existing else 0
                if existing is not None and _semantic_payload(existing) == _semantic_payload(event):
                    continue
                sequence = await self._next_sequence(session)
                published = event.model_copy(update={"revision": revision + 1})
                await session.execute(
                    """
                    INSERT INTO canonical_income_events (
                        event_id, ticker, ex_date, payment_date, status,
                        revision, payload, changed_seq
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(event_id) DO UPDATE SET
                        ticker = excluded.ticker,
                        ex_date = excluded.ex_date,
                        payment_date = excluded.payment_date,
                        status = excluded.status,
                        revision = excluded.revision,
                        payload = excluded.payload,
                        changed_seq = excluded.changed_seq
                    """,
                    (
                        published.event_id,
                        published.ticker,
                        published.ex_date.isoformat(),
                        published.payment_date.isoformat(),
                        published.status.value,
                        published.revision,
                        _dump(published),
                        sequence,
                    ),
                )
                changed += 1
            changed += await self._cancel_missing(session, events, scope_tickers or [])
        return changed

    async def _cancel_missing(
        self,
        session: _Session,
        events: list[CanonicalIncomeEvent],
        scope_tickers: list[str],
    ) -> int:
        if not scope_tickers:
            return 0
        expected = {item.event_id for item in events}
        placeholders = ",".join("?" for _ in scope_tickers)
        query = (
            "SELECT payload FROM canonical_income_events WHERE "
            f"ticker IN ({placeholders}) AND status != ?"
        )  # noqa: S608 - placeholders are generated, never user-controlled
        params = [*(ticker.upper() for ticker in scope_tickers), IncomeEventStatus.cancelled.value]
        rows = await session.fetchall(query, params)
        existing = [CanonicalIncomeEvent.model_validate_json(row["payload"]) for row in rows]
        changed = 0
        for event in existing:
            if event.event_id in expected:
                continue
            sequence = await self._next_sequence(session)
            cancelled = event.model_copy(
                update={
                    "status": IncomeEventStatus.cancelled,
                    "revision": event.revision + 1,
                    "updated_at": datetime.now(UTC),
                }
            )
            await session.execute(
                """
                UPDATE canonical_income_events
                SET status = ?, revision = ?, payload = ?, changed_seq = ?
                WHERE event_id = ?
                """,
                (
                    cancelled.status.value,
                    cancelled.revision,
                    _dump(cancelled),
                    sequence,
                    cancelled.event_id,
                ),
            )
            changed += 1
        return changed

    async def events(
        self,
        tickers: list[str],
        *,
        from_date: date | None = None,
        to_date: date | None = None,
        include_tentative: bool = False,
    ) -> list[CanonicalIncomeEvent]:
        if not tickers:
            return []
        placeholders = ",".join("?" for _ in tickers)
        filters = [f"ticker IN ({placeholders})"]  # noqa: S608 - fixed placeholders
        params: list[object] = [ticker.upper() for ticker in tickers]
        if from_date is not None:
            filters.append("payment_date >= ?")
            params.append(from_date.isoformat())
        if to_date is not None:
            filters.append("payment_date <= ?")
            params.append(to_date.isoformat())
        visible_statuses = (
            (
                IncomeEventStatus.tentative.value,
                IncomeEventStatus.corroborated.value,
                IncomeEventStatus.verified.value,
            )
            if include_tentative
            else (
                IncomeEventStatus.corroborated.value,
                IncomeEventStatus.verified.value,
            )
        )
        status_placeholders = ",".join("?" for _status in visible_statuses)
        filters.append(f"status IN ({status_placeholders})")  # noqa: S608 - fixed placeholders
        params.extend(visible_statuses)
        query = (
            "SELECT payload FROM canonical_income_events WHERE "
            + " AND ".join(filters)
            + " ORDER BY ticker, payment_date, event_id"
        )
        async with self._read_session() as session:
            rows = await session.fetchall(query, params)
        return [CanonicalIncomeEvent.model_validate_json(row["payload"]) for row in rows]

    async def changes(
        self,
        cursor: int,
        *,
        limit: int,
    ) -> tuple[list[CanonicalIncomeEvent], int, bool]:
        query = """
            SELECT payload, changed_seq FROM canonical_income_events
            WHERE changed_seq > ? ORDER BY changed_seq LIMIT ?
        """
        async with self._read_session() as session:
            rows = await session.fetchall(query, (max(cursor, 0), limit + 1))
        has_more = len(rows) > limit
        selected = rows[:limit]
        next_cursor = int(selected[-1]["changed_seq"]) if selected else max(cursor, 0)
        events = [CanonicalIncomeEvent.model_validate_json(row["payload"]) for row in selected]
        return events, next_cursor, has_more

    async def cursor(self) -> int:
        async with self._read_session() as session:
            row = await session.fetchone(
                "SELECT value FROM income_event_sequence WHERE singleton = 1"
            )
        return int(row["value"]) if row else 0

    async def create_refresh_job(
        self,
        job_id: str,
        items: list[tuple[str, str]],
        *,
        requested: int,
        as_of: date,
        now: datetime,
    ) -> int:
        """Create a job and enqueue only the items no other job owns."""
        moment = now.isoformat()
        inserted = 0
        async with self._write_session() as session:
            await session.execute(
                """
                INSERT INTO income_refresh_jobs (
                    job_id, status, requested, as_of, error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, NULL, ?, ?)
                """,
                (job_id, REFRESH_QUEUED, requested, as_of.isoformat(), moment, moment),
            )
            for source, ticker in items:
                inserted += await session.insert_ignore(
                    _JOB_ITEM_INSERT,
                    (job_id, source, ticker.upper(), ITEM_QUEUED, moment),
                )
        return inserted

    async def claim_refresh_items(
        self,
        *,
        limit: int,
        lease_seconds: int,
        now: datetime,
    ) -> list[dict[str, object]]:
        moment = now.isoformat()
        lease = (now + timedelta(seconds=lease_seconds)).isoformat()
        order_column = "id" if self.postgres else "rowid"
        lock_clause = " FOR UPDATE OF i SKIP LOCKED" if self.postgres else ""
        select = f"""
            SELECT i.job_id, i.source, i.ticker, i.attempts + 1 AS attempts, j.as_of
            FROM income_refresh_job_items i
            JOIN income_refresh_jobs j ON j.job_id = i.job_id
            WHERE i.status = ?
              AND (i.lease_until IS NULL OR i.lease_until <= ?)
            ORDER BY i.{order_column}
            LIMIT ?{lock_clause}
        """  # noqa: S608 - the ordering column is a fixed literal
        async with self._write_session() as session:
            await session.execute(
                """
                UPDATE income_refresh_job_items
                SET status = ?, lease_until = NULL, updated_at = ?
                WHERE status = ? AND lease_until IS NOT NULL AND lease_until <= ?
                """,
                (ITEM_QUEUED, moment, ITEM_RUNNING, moment),
            )
            rows = await session.fetchall(select, (ITEM_QUEUED, moment, limit))
            if rows:
                await session.executemany(
                    """
                    UPDATE income_refresh_job_items
                    SET status = ?, attempts = attempts + 1, lease_until = ?, updated_at = ?
                    WHERE job_id = ? AND source = ? AND ticker = ?
                    """,
                    [
                        (
                            ITEM_RUNNING,
                            lease,
                            moment,
                            row["job_id"],
                            row["source"],
                            row["ticker"],
                        )
                        for row in rows
                    ],
                )
                await session.executemany(
                    """
                    UPDATE income_refresh_jobs SET status = ?, updated_at = ?
                    WHERE job_id = ?
                    """,
                    [
                        (REFRESH_RUNNING, moment, job_id)
                        for job_id in {str(row["job_id"]) for row in rows}
                    ],
                )
        return [dict(row) for row in rows]

    async def complete_refresh_item(
        self,
        job_id: str,
        source: str,
        ticker: str,
        *,
        now: datetime,
    ) -> None:
        await self._set_refresh_item(
            job_id,
            source,
            ticker,
            status=ITEM_COMPLETE,
            lease_until=None,
            last_error=None,
            now=now,
        )

    async def requeue_refresh_item(
        self,
        job_id: str,
        source: str,
        ticker: str,
        *,
        error: str,
        available_at: datetime,
        now: datetime,
    ) -> None:
        await self._set_refresh_item(
            job_id,
            source,
            ticker,
            status=ITEM_QUEUED,
            lease_until=available_at,
            last_error=error[:200],
            now=now,
        )

    async def fail_refresh_item(
        self,
        job_id: str,
        source: str,
        ticker: str,
        *,
        error: str,
        now: datetime,
    ) -> None:
        await self._set_refresh_item(
            job_id,
            source,
            ticker,
            status=ITEM_FAILED,
            lease_until=None,
            last_error=error[:200],
            now=now,
        )

    async def _set_refresh_item(
        self,
        job_id: str,
        source: str,
        ticker: str,
        *,
        status: str,
        lease_until: datetime | None,
        last_error: str | None,
        now: datetime,
    ) -> None:
        async with self._write_session() as session:
            await session.execute(
                """
                UPDATE income_refresh_job_items
                SET status = ?, lease_until = ?, last_error = ?, updated_at = ?
                WHERE job_id = ? AND source = ? AND ticker = ?
                """,
                (
                    status,
                    lease_until.isoformat() if lease_until is not None else None,
                    last_error,
                    now.isoformat(),
                    job_id,
                    source,
                    ticker.upper(),
                ),
            )

    async def pending_refresh_item_count(self, job_id: str) -> int:
        async with self._read_session() as session:
            row = await session.fetchone(
                """
                SELECT COUNT(*) AS total FROM income_refresh_job_items
                WHERE job_id = ? AND status IN (?, ?)
                """,
                (job_id, ITEM_QUEUED, ITEM_RUNNING),
            )
        return int(row["total"]) if row else 0

    async def job_tickers(self, job_id: str) -> list[str]:
        async with self._read_session() as session:
            rows = await session.fetchall(
                """
                SELECT DISTINCT ticker FROM income_refresh_job_items
                WHERE job_id = ? ORDER BY ticker
                """,
                (job_id,),
            )
        return [str(row["ticker"]) for row in rows]

    async def finish_refresh_job(
        self,
        job_id: str,
        *,
        status: str,
        error: str | None,
        now: datetime,
    ) -> None:
        async with self._write_session() as session:
            await session.execute(
                """
                UPDATE income_refresh_jobs SET status = ?, error = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (status, error[:200] if error else None, now.isoformat(), job_id),
            )

    async def refresh_job(self, job_id: str) -> dict[str, object] | None:
        async with self._read_session() as session:
            job = await session.fetchone(
                "SELECT * FROM income_refresh_jobs WHERE job_id = ?", (job_id,)
            )
            if job is None:
                return None
            items = await session.fetchall(
                """
                SELECT source, ticker, status, attempts, last_error
                FROM income_refresh_job_items WHERE job_id = ?
                ORDER BY source, ticker
                """,
                (job_id,),
            )
        return {
            "job_id": str(job["job_id"]),
            "status": str(job["status"]),
            "requested": int(job["requested"]),
            "error": job["error"],
            "created_at": str(job["created_at"]),
            "updated_at": str(job["updated_at"]),
            "completed": sum(1 for item in items if item["status"] == ITEM_COMPLETE),
            "failed": sum(1 for item in items if item["status"] == ITEM_FAILED),
            "items": [dict(item) for item in items],
        }

    async def coverage(self, tickers: list[str]) -> list[IncomeSourceCoverage]:
        if not tickers:
            return []
        placeholders = ",".join("?" for _ in tickers)
        query = (
            "SELECT payload FROM income_source_coverage "
            f"WHERE ticker IN ({placeholders}) ORDER BY ticker, source"
        )  # noqa: S608 - placeholders are generated, never user-controlled
        async with self._read_session() as session:
            rows = await session.fetchall(query, [ticker.upper() for ticker in tickers])
        return [IncomeSourceCoverage.model_validate_json(row["payload"]) for row in rows]

    async def _existing(
        self,
        session: _Session,
        event_id: str,
    ) -> CanonicalIncomeEvent | None:
        row = await session.fetchone(
            "SELECT payload FROM canonical_income_events WHERE event_id = ?", (event_id,)
        )
        return CanonicalIncomeEvent.model_validate_json(row["payload"]) if row else None

    async def _next_sequence(self, session: _Session) -> int:
        await session.execute(
            "UPDATE income_event_sequence SET value = value + 1 WHERE singleton = 1"
        )
        row = await session.fetchone("SELECT value FROM income_event_sequence WHERE singleton = 1")
        if row is None:
            raise RuntimeError("income event sequence is unavailable")
        return int(row["value"])

    def _require_db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("IncomeEventStore was not started")
        return self._db

    @asynccontextmanager
    async def _read_session(self) -> AsyncIterator[_Session]:
        async with self._lock:
            if self.postgres:
                connection = await postgres_connect(self.database_url)
                try:
                    async with connection.transaction():
                        yield _Session(connection, postgres=True)
                finally:
                    await connection.close()
                return
            yield _Session(self._require_db(), postgres=False)

    @asynccontextmanager
    async def _write_session(self) -> AsyncIterator[_Session]:
        if self.postgres:
            async with self._read_session() as session:
                yield session
            return
        async with self._lock:
            db = self._require_db()
            async with sqlite_transaction(db, self.path):
                yield _Session(db, postgres=False)

    async def _postgres_setup(self) -> None:
        connection = await postgres_connect(self.database_url)
        try:
            async with connection.transaction():
                for statement in _POSTGRES_SCHEMA:
                    await connection.execute(statement)
        finally:
            await connection.close()

    async def _ensure_observation_active_column(self) -> None:
        db = self._require_db()
        async with db.execute("PRAGMA table_info(income_event_observations)") as cursor:
            columns = {str(row["name"]) for row in await cursor.fetchall()}
        if "active" not in columns:
            await db.execute(
                "ALTER TABLE income_event_observations ADD COLUMN active INTEGER NOT NULL DEFAULT 1"
            )

    async def _ensure_observation_payment_date_column(self) -> None:
        db = self._require_db()
        async with db.execute("PRAGMA table_info(income_event_observations)") as cursor:
            columns = {str(row["name"]) for row in await cursor.fetchall()}
        if "payment_date" not in columns:
            await db.execute("ALTER TABLE income_event_observations ADD COLUMN payment_date TEXT")


def _observation_row(item: IncomeEventObservation) -> tuple[object, ...]:
    return (
        item.source,
        item.source_event_id,
        item.source_version,
        item.ticker.upper(),
        item.payment_date.isoformat() if item.payment_date is not None else None,
        _dump(item),
        item.observed_at.isoformat(),
        1,
    )


def _dump(value: object) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return orjson.dumps(value).decode()


def _semantic_payload(event: CanonicalIncomeEvent) -> dict[str, object]:
    payload = event.model_dump(mode="json")
    payload.pop("revision", None)
    payload.pop("updated_at", None)
    return payload


__all__ = ["IncomeEventStore"]
