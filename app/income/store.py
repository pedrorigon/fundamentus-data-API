from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from pathlib import Path

import aiosqlite
import orjson

from app.models.income_events import (
    CanonicalIncomeEvent,
    IncomeEventObservation,
    IncomeEventStatus,
    IncomeSourceCoverage,
)


class IncomeEventStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def startup(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS income_event_observations (
                source TEXT NOT NULL,
                source_event_id TEXT NOT NULL,
                source_version INTEGER NOT NULL,
                ticker TEXT NOT NULL,
                payload TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                PRIMARY KEY (source, source_event_id, source_version)
            );
            CREATE INDEX IF NOT EXISTS ix_income_observation_ticker
                ON income_event_observations (ticker, observed_at);

            CREATE TABLE IF NOT EXISTS canonical_income_events (
                event_id TEXT PRIMARY KEY,
                ticker TEXT NOT NULL,
                ex_date TEXT NOT NULL,
                payment_date TEXT NOT NULL,
                status TEXT NOT NULL,
                revision INTEGER NOT NULL,
                payload TEXT NOT NULL,
                changed_seq INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_canonical_income_ticker_payment
                ON canonical_income_events (ticker, payment_date);
            CREATE INDEX IF NOT EXISTS ix_canonical_income_changes
                ON canonical_income_events (changed_seq);

            CREATE TABLE IF NOT EXISTS income_source_coverage (
                source TEXT NOT NULL,
                ticker TEXT NOT NULL,
                payload TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                PRIMARY KEY (source, ticker)
            );

            CREATE TABLE IF NOT EXISTS income_event_sequence (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                value INTEGER NOT NULL
            );
            INSERT OR IGNORE INTO income_event_sequence (singleton, value) VALUES (1, 0);
            """
        )
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def save_observations(self, observations: list[IncomeEventObservation]) -> int:
        if not observations:
            return 0
        db = self._require_db()
        rows = [
            (
                item.source,
                item.source_event_id,
                item.source_version,
                item.ticker.upper(),
                _dump(item),
                item.observed_at.isoformat(),
            )
            for item in observations
        ]
        async with self._lock:
            await db.executemany(
                """
                INSERT INTO income_event_observations (
                    source, source_event_id, source_version, ticker, payload, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, source_event_id, source_version) DO UPDATE SET
                    ticker = excluded.ticker,
                    payload = excluded.payload,
                    observed_at = excluded.observed_at
                """,
                rows,
            )
            await db.commit()
        return len(rows)

    async def observations(self, tickers: list[str]) -> list[IncomeEventObservation]:
        if not tickers:
            return []
        db = self._require_db()
        placeholders = ",".join("?" for _ in tickers)
        query = f"""
            SELECT payload FROM (
                SELECT payload, source, source_event_id, observed_at,
                       ROW_NUMBER() OVER (
                           PARTITION BY source, source_event_id
                           ORDER BY source_version DESC, observed_at DESC
                       ) AS version_rank
                FROM income_event_observations
                WHERE ticker IN ({placeholders})
            ) latest
            WHERE version_rank = 1
            ORDER BY observed_at, source, source_event_id
        """  # noqa: S608 - placeholders are generated, never user-controlled
        async with db.execute(query, [ticker.upper() for ticker in tickers]) as cursor:
            rows = await cursor.fetchall()
        return [IncomeEventObservation.model_validate_json(row["payload"]) for row in rows]

    async def save_coverage(self, coverage: list[IncomeSourceCoverage]) -> None:
        if not coverage:
            return
        db = self._require_db()
        async with self._lock:
            await db.executemany(
                """
                INSERT INTO income_source_coverage (source, ticker, payload, observed_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(source, ticker) DO UPDATE SET
                    payload = excluded.payload,
                    observed_at = excluded.observed_at
                """,
                [
                    (item.source, item.ticker.upper(), _dump(item), item.observed_at.isoformat())
                    for item in coverage
                ],
            )
            await db.commit()

    async def publish(
        self,
        events: list[CanonicalIncomeEvent],
        *,
        scope_tickers: list[str] | None = None,
    ) -> int:
        if not events and not scope_tickers:
            return 0
        db = self._require_db()
        changed = 0
        async with self._lock:
            for event in events:
                existing = await self._existing(event.event_id)
                revision = existing.revision if existing else 0
                if existing is not None and _semantic_payload(existing) == _semantic_payload(event):
                    continue
                sequence = await self._next_sequence()
                published = event.model_copy(update={"revision": revision + 1})
                await db.execute(
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
            changed += await self._cancel_missing(events, scope_tickers or [])
            await db.commit()
        return changed

    async def _cancel_missing(
        self,
        events: list[CanonicalIncomeEvent],
        scope_tickers: list[str],
    ) -> int:
        if not scope_tickers:
            return 0
        db = self._require_db()
        expected = {item.event_id for item in events}
        placeholders = ",".join("?" for _ in scope_tickers)
        query = (
            "SELECT payload FROM canonical_income_events WHERE "
            f"ticker IN ({placeholders}) AND status != ?"
        )  # noqa: S608 - placeholders are generated, never user-controlled
        params = [*(ticker.upper() for ticker in scope_tickers), IncomeEventStatus.cancelled.value]
        async with db.execute(query, params) as cursor:
            existing = [
                CanonicalIncomeEvent.model_validate_json(row["payload"])
                for row in await cursor.fetchall()
            ]
        changed = 0
        for event in existing:
            if event.event_id in expected:
                continue
            sequence = await self._next_sequence()
            cancelled = event.model_copy(
                update={
                    "status": IncomeEventStatus.cancelled,
                    "revision": event.revision + 1,
                    "updated_at": datetime.now(UTC),
                }
            )
            await db.execute(
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
        db = self._require_db()
        placeholders = ",".join("?" for _ in tickers)
        filters = [f"ticker IN ({placeholders})"]  # noqa: S608 - fixed placeholders
        params: list[object] = [ticker.upper() for ticker in tickers]
        if from_date is not None:
            filters.append("payment_date >= ?")
            params.append(from_date.isoformat())
        if to_date is not None:
            filters.append("payment_date <= ?")
            params.append(to_date.isoformat())
        if not include_tentative:
            filters.append("status IN (?, ?)")
            params.extend((IncomeEventStatus.corroborated.value, IncomeEventStatus.verified.value))
        query = (
            "SELECT payload FROM canonical_income_events WHERE "
            + " AND ".join(filters)
            + " ORDER BY ticker, payment_date, event_id"
        )
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
        return [CanonicalIncomeEvent.model_validate_json(row["payload"]) for row in rows]

    async def changes(
        self,
        cursor: int,
        *,
        limit: int,
    ) -> tuple[list[CanonicalIncomeEvent], int, bool]:
        db = self._require_db()
        async with db.execute(
            """
            SELECT payload, changed_seq FROM canonical_income_events
            WHERE changed_seq > ? ORDER BY changed_seq LIMIT ?
            """,
            (max(cursor, 0), limit + 1),
        ) as result:
            rows = list(await result.fetchall())
        has_more = len(rows) > limit
        selected = rows[:limit]
        next_cursor = int(selected[-1]["changed_seq"]) if selected else max(cursor, 0)
        events = [CanonicalIncomeEvent.model_validate_json(row["payload"]) for row in selected]
        return events, next_cursor, has_more

    async def cursor(self) -> int:
        db = self._require_db()
        async with db.execute(
            "SELECT value FROM income_event_sequence WHERE singleton = 1"
        ) as cursor:
            row = await cursor.fetchone()
        return int(row["value"]) if row else 0

    async def _existing(self, event_id: str) -> CanonicalIncomeEvent | None:
        db = self._require_db()
        async with db.execute(
            "SELECT payload FROM canonical_income_events WHERE event_id = ?", (event_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return CanonicalIncomeEvent.model_validate_json(row["payload"]) if row else None

    async def _next_sequence(self) -> int:
        db = self._require_db()
        await db.execute("UPDATE income_event_sequence SET value = value + 1 WHERE singleton = 1")
        async with db.execute(
            "SELECT value FROM income_event_sequence WHERE singleton = 1"
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("income event sequence is unavailable")
        return int(row["value"])

    def _require_db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("IncomeEventStore was not started")
        return self._db


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
