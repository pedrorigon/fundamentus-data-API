from __future__ import annotations

import asyncio
import json
import secrets
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from app.assessment.models import (
    ASSESSMENT_TIMEZONE,
    AssessmentComponentStatus,
    AssessmentRunStatus,
    AssessmentSnapshotResponse,
)
from app.core.postgres import (
    normalize_database_url,
    postgres_connect,
    postgres_row_factory,
)


# Thin aliases keep the historical private names importable and patchable
# while the shared implementation lives in one place.
def _normalize_database_url(value: str | None) -> str | None:
    return normalize_database_url(value)


async def _postgres_connect(database_url: str | None) -> Any:
    return await postgres_connect(database_url)


def _postgres_row_factory(cursor: Any) -> Any:
    return postgres_row_factory(cursor)


ASSESSMENT_TABLE = "fundamentus_assessment_snapshots"
MAX_LEASE_SECONDS = 3600
ASSESSMENT_MAINTENANCE_BATCH_SIZE = 100


class AssessmentStoreError(RuntimeError):
    """Raised when durable assessment ownership cannot be established."""


@dataclass(frozen=True)
class AssessmentRecord:
    key: str
    ticker: str
    kind: str
    profile: str | None
    venue: str | None
    period_at: datetime
    status: AssessmentRunStatus
    attempts: int
    generation: int
    lease_token: str | None
    lease_expires_at: datetime | None
    retry_at: datetime | None
    payload: dict[str, Any] | None
    error: str | None
    evidence_digest: str | None
    fetched_at: datetime | None
    last_good_payload: dict[str, Any] | None = None
    last_good_evidence_digest: str | None = None
    last_good_fetched_at: datetime | None = None

    def response(self) -> AssessmentSnapshotResponse | None:
        if self.payload is None:
            return None
        return AssessmentSnapshotResponse.model_validate(self.payload)

    def last_good_response(self) -> AssessmentSnapshotResponse | None:
        if self.last_good_payload is None:
            return None
        return AssessmentSnapshotResponse.model_validate(self.last_good_payload)


@dataclass(frozen=True)
class ClaimResult:
    state: str
    record: AssessmentRecord
    token: str | None = None
    generation: int | None = None


class AssessmentStore:
    """Durable idempotency and snapshot storage.

    SQLite is deliberately separate from the source-data cache.  PostgreSQL is
    selected when ``database_url`` is configured; the implementation opens a
    short-lived connection for each transaction and never holds a database
    transaction while a source provider is awaited.
    """

    def __init__(
        self,
        sqlite_path: Path | None = None,
        *,
        database_url: str | None = None,
        default_lease_seconds: int = 1800,
        max_attempts: int = 3,
    ) -> None:
        self.sqlite_path = sqlite_path or Path(".cache/fundamentus_assessments.sqlite3")
        self.database_url = _normalize_database_url(database_url)
        self.default_lease_seconds = min(MAX_LEASE_SECONDS, max(1, default_lease_seconds))
        self.max_attempts = max(1, max_attempts)
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._started = False

    async def startup(self) -> None:
        async with self._lifecycle_lock:
            if self._started:
                return
            if self.database_url:
                await self._postgres_setup()
            else:
                self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
                db = await aiosqlite.connect(self.sqlite_path)
                try:
                    db.row_factory = aiosqlite.Row
                    await db.execute("PRAGMA busy_timeout = 5000")
                    await db.execute("PRAGMA journal_mode = WAL")
                    await db.execute("PRAGMA synchronous = NORMAL")
                    await db.executescript(_SCHEMA)
                    for statement in _SQLITE_MIGRATIONS:
                        try:
                            await db.execute(statement)
                        except aiosqlite.OperationalError as exc:
                            if "duplicate column name" not in str(exc).lower():
                                raise
                    await db.execute(_SQLITE_INDEX)
                    await db.commit()
                except BaseException:
                    await db.close()
                    raise
                self._db = db
            self._started = True

    async def close(self) -> None:
        async with self._lifecycle_lock:
            db = self._db
            if db is None:
                self._started = False
                return
            # SQLite uses one long-lived connection.  Acquire the operation
            # lock before detaching it so close cannot interrupt an in-flight
            # transaction (or a queued read) on the connection's worker
            # thread. Operations resolve the connection after acquiring this
            # lock, so callers queued behind close observe the detached state
            # and fail cleanly.
            async with self._lock:
                self._db = None
                self._started = False
                await db.close()

    async def get(self, key: str) -> AssessmentRecord | None:
        self._ensure_started()
        if self.database_url:
            row = await self._postgres_fetchone(
                f"SELECT * FROM {ASSESSMENT_TABLE} WHERE run_key = %s",
                (key,),
            )
        else:
            async with self._lock:
                db = self._require_db()
                async with db.execute(
                    f"SELECT * FROM {ASSESSMENT_TABLE} WHERE run_key = ?", (key,)
                ) as cur:
                    row = await cur.fetchone()
        return _record(row) if row is not None else None

    async def cleanup_before(self, before: datetime, *, batch_size: int = 500) -> int:
        """Delete old terminal snapshots in bounded maintenance batches.

        This method is intentionally separate from ``get`` and ``claim`` so a
        read never acquires a write lock or silently changes retention state.
        Rows still being processed are retained for lease recovery.
        """

        self._ensure_started()
        cutoff = _utc(before)
        limit = max(1, batch_size)
        if self.database_url:
            return await self._postgres_cleanup_before(cutoff, limit)
        removed = 0
        async with self._lock:
            db = self._require_db()
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    f"""
                    DELETE FROM {ASSESSMENT_TABLE}
                    WHERE rowid IN (
                        SELECT rowid FROM {ASSESSMENT_TABLE}
                        WHERE period_at < ? AND status != 'processing'
                        ORDER BY period_at
                        LIMIT ?
                    )
                    """,
                    (cutoff.isoformat(), limit),
                )
                removed = max(0, cursor.rowcount)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return removed

    async def terminalize_expired(
        self,
        *,
        now: datetime | None = None,
        batch_size: int = ASSESSMENT_MAINTENANCE_BATCH_SIZE,
    ) -> int:
        """Fence expired claims whose scheduler period is already closed.

        A worker may disappear after acquiring a lease.  Keeping that row in
        ``processing`` forever blocks retention cleanup and makes readers wait
        for an ownership transition that can no longer happen.  Maintenance
        only terminalizes claims after their immutable capture window closes;
        an expired lease for the currently open period remains reclaimable by
        the normal claim path.
        """

        self._ensure_started()
        current = _utc(now or datetime.now(UTC))
        limit = max(1, batch_size)
        if self.database_url:
            return await self._postgres_terminalize_expired(current, limit)
        terminalized = 0
        async with self._lock:
            db = self._require_db()
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    f"""
                    SELECT * FROM {ASSESSMENT_TABLE}
                    WHERE status = 'processing'
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at <= ?
                    ORDER BY period_at, run_key
                    LIMIT ?
                    """,
                    (current.isoformat(), limit),
                ) as cur:
                    rows = await cur.fetchall()
                for row in rows:
                    record = _record(row)
                    if not _assessment_period_closed(record.period_at, current):
                        continue
                    cursor = await db.execute(
                        f"""
                        UPDATE {ASSESSMENT_TABLE}
                        SET status = 'failed', error = 'lease_expired',
                            lease_token = NULL, lease_expires_at = NULL,
                            retry_at = NULL, updated_at = ?
                        WHERE run_key = ? AND status = 'processing'
                          AND lease_token = ? AND generation = ?
                          AND lease_expires_at <= ?
                        """,
                        (
                            current.isoformat(),
                            record.key,
                            record.lease_token,
                            record.generation,
                            current.isoformat(),
                        ),
                    )
                    terminalized += max(0, cursor.rowcount)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return terminalized

    async def maintenance(
        self,
        before: datetime,
        *,
        now: datetime | None = None,
        batch_size: int = ASSESSMENT_MAINTENANCE_BATCH_SIZE,
    ) -> tuple[int, int]:
        """Run one short, bounded lease and retention maintenance pass."""

        current = _utc(now or datetime.now(UTC))
        limit = max(1, batch_size)
        terminalized = await self.terminalize_expired(now=current, batch_size=limit)
        removed = await self.cleanup_before(before, batch_size=limit)
        return terminalized, removed

    async def claim(
        self,
        *,
        key: str,
        ticker: str,
        kind: str,
        profile: str | None,
        period_at: datetime,
        venue: str | None = None,
        now: datetime | None = None,
        lease_seconds: int | None = None,
    ) -> ClaimResult:
        """Claim one period before any network I/O.

        A completed row always wins.  A live lease is returned as
        ``in_progress``.  An expired lease can be reclaimed with a new token
        and generation, which prevents a late worker from publishing stale
        data.
        """

        self._ensure_started()
        current = _utc(now or datetime.now(UTC))
        duration = min(MAX_LEASE_SECONDS, max(1, lease_seconds or self.default_lease_seconds))
        expires = current + timedelta(seconds=duration)
        token = secrets.token_urlsafe(24)

        if self.database_url:
            return await self._postgres_claim(
                key, ticker, kind, profile, venue, period_at, current, expires, token
            )

        async with self._lock:
            db = self._require_db()
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    f"SELECT * FROM {ASSESSMENT_TABLE} WHERE run_key = ?", (key,)
                ) as cur:
                    existing_row = await cur.fetchone()
                existing = _record(existing_row) if existing_row is not None else None
                result = _claim_decision(
                    existing,
                    key=key,
                    ticker=ticker,
                    kind=kind,
                    profile=profile,
                    venue=venue,
                    period_at=period_at,
                    now=current,
                    expires=expires,
                    token=token,
                    max_attempts=self.max_attempts,
                )
                if result.token is not None or _terminalization_required(existing, result):
                    await db.execute(
                        _UPSERT_SQLITE,
                        _record_params(
                            result.record,
                            lease_token=result.token,
                        ),
                    )
                await db.commit()
                return result
            except BaseException:
                await db.rollback()
                raise

    async def publish(
        self,
        *,
        key: str,
        token: str,
        generation: int,
        response: AssessmentSnapshotResponse,
        now: datetime | None = None,
    ) -> bool:
        """Publish a result only if this worker still owns the lease."""

        self._ensure_started()
        payload = json.dumps(response.model_dump(mode="json"), separators=(",", ":"))
        fetched_at = response.fetched_at.isoformat() if response.fetched_at else None
        current = _utc(now or datetime.now(UTC))
        if self.database_url:
            return await self._postgres_publish(
                key,
                token,
                generation,
                payload,
                response.evidence_digest,
                fetched_at,
                current,
                _is_publishable(response),
            )
        async with self._lock:
            db = self._require_db()
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    f"""
                    UPDATE {ASSESSMENT_TABLE}
                    SET status = 'completed', payload = ?, fetched_at = ?,
                        last_good_payload = COALESCE(?, last_good_payload),
                        last_good_fetched_at = COALESCE(?, last_good_fetched_at),
                        last_good_evidence_digest = COALESCE(?, last_good_evidence_digest),
                        evidence_digest = ?,
                        lease_token = NULL,
                        lease_expires_at = NULL, retry_at = NULL, error = NULL,
                        updated_at = ?
                    WHERE run_key = ? AND status = 'processing'
                      AND lease_token = ? AND generation = ?
                      AND lease_expires_at > ?
                    """,
                    (
                        payload,
                        fetched_at,
                        payload if _is_publishable(response) else None,
                        fetched_at if _is_publishable(response) else None,
                        response.evidence_digest if _is_publishable(response) else None,
                        response.evidence_digest,
                        current.isoformat(),
                        key,
                        token,
                        generation,
                        current.isoformat(),
                    ),
                )
                published = cursor.rowcount == 1
                await db.commit()
                return published
            except BaseException:
                await db.rollback()
                raise

    async def fail(
        self,
        *,
        key: str,
        token: str,
        generation: int,
        response: AssessmentSnapshotResponse,
        retry_after_seconds: int,
        now: datetime | None = None,
    ) -> bool:
        """Persist a typed failure without replacing a completed result."""

        self._ensure_started()
        payload = json.dumps(response.model_dump(mode="json"), separators=(",", ":"))
        current = _utc(now or datetime.now(UTC))
        retry_at = current + timedelta(seconds=max(0, retry_after_seconds))
        if self.database_url:
            return await self._postgres_fail(
                key,
                token,
                generation,
                payload,
                response.error,
                response.evidence_digest,
                retry_at,
                current,
            )
        async with self._lock:
            db = self._require_db()
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    f"""
                    UPDATE {ASSESSMENT_TABLE}
                    SET status = 'failed', payload = ?, error = ?,
                        evidence_digest = ?, fetched_at = NULL,
                        lease_token = NULL, lease_expires_at = NULL,
                        retry_at = ?, updated_at = ?
                    WHERE run_key = ? AND status = 'processing'
                      AND lease_token = ? AND generation = ?
                      AND lease_expires_at > ?
                    """,
                    (
                        payload,
                        response.error,
                        response.evidence_digest,
                        retry_at.isoformat(),
                        current.isoformat(),
                        key,
                        token,
                        generation,
                        current.isoformat(),
                    ),
                )
                saved = cursor.rowcount == 1
                await db.commit()
                return saved
            except BaseException:
                await db.rollback()
                raise

    def _ensure_started(self) -> None:
        if not self._started:
            raise RuntimeError("AssessmentStore.startup() must be called first")

    def _require_db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("SQLite assessment store is not open")
        return self._db

    async def _postgres_setup(self) -> None:
        async with await _postgres_connect(self.database_url) as conn:
            await conn.execute(_POSTGRES_SCHEMA)
            for statement in _POSTGRES_MIGRATIONS:
                await conn.execute(statement)
            # Legacy installations may not have ``venue`` yet.  Keep index
            # creation after additive migrations so PostgreSQL can initialize
            # both fresh and upgraded databases in one startup transaction.
            await conn.execute(_POSTGRES_INDEX)
            await conn.commit()

    async def _postgres_fetchone(self, query: str, params: tuple[Any, ...]) -> Any:
        async with await _postgres_connect(self.database_url) as conn:
            async with conn.cursor(row_factory=_postgres_row_factory) as cur:
                await cur.execute(query, params)
                return await cur.fetchone()

    async def _postgres_cleanup_before(self, cutoff: datetime, limit: int) -> int:
        async with await _postgres_connect(self.database_url) as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""
                        DELETE FROM {ASSESSMENT_TABLE}
                        WHERE ctid IN (
                            SELECT ctid FROM {ASSESSMENT_TABLE}
                            WHERE period_at < %s AND status != 'processing'
                            ORDER BY period_at
                            LIMIT %s
                        )
                        """,
                        (cutoff, limit),
                    )
                    return max(0, int(cur.rowcount))

    async def _postgres_terminalize_expired(self, now: datetime, limit: int) -> int:
        async with await _postgres_connect(self.database_url) as conn:
            async with conn.transaction():
                async with conn.cursor(row_factory=_postgres_row_factory) as cur:
                    await cur.execute(
                        f"""
                        SELECT * FROM {ASSESSMENT_TABLE}
                        WHERE status = 'processing'
                          AND lease_expires_at IS NOT NULL
                          AND lease_expires_at <= %s
                        ORDER BY period_at, run_key
                        LIMIT %s
                        FOR UPDATE SKIP LOCKED
                        """,
                        (now, limit),
                    )
                    rows = await cur.fetchall()
                    terminalized = 0
                    for row in rows:
                        record = _record(row)
                        if not _assessment_period_closed(record.period_at, now):
                            continue
                        await cur.execute(
                            f"""
                            UPDATE {ASSESSMENT_TABLE}
                            SET status = 'failed', error = 'lease_expired',
                                lease_token = NULL, lease_expires_at = NULL,
                                retry_at = NULL, updated_at = %s
                            WHERE run_key = %s AND status = 'processing'
                              AND lease_token = %s AND generation = %s
                              AND lease_expires_at <= %s
                            """,
                            (
                                now,
                                record.key,
                                record.lease_token,
                                record.generation,
                                now,
                            ),
                        )
                        terminalized += max(0, int(cur.rowcount))
                    return terminalized

    async def _postgres_claim(
        self,
        key: str,
        ticker: str,
        kind: str,
        profile: str | None,
        venue: str | None,
        period_at: datetime,
        now: datetime,
        expires: datetime,
        token: str,
    ) -> ClaimResult:
        async with await _postgres_connect(self.database_url) as conn:
            async with conn.transaction():
                async with conn.cursor(row_factory=_postgres_row_factory) as cur:
                    # A ``SELECT ... FOR UPDATE`` cannot lock a row that does
                    # not exist.  Two workers could therefore both observe a
                    # missing key, perform the provider I/O, and only race
                    # when they later upsert the claim.  Inserting the first
                    # processing row with ``ON CONFLICT DO NOTHING`` makes
                    # the unique key the serialization point.  The statement
                    # waits for any concurrent insert before deciding whether
                    # this transaction owns the new row.
                    first_claim = _claim_decision(
                        None,
                        key=key,
                        ticker=ticker,
                        kind=kind,
                        profile=profile,
                        venue=venue,
                        period_at=period_at,
                        now=now,
                        expires=expires,
                        token=token,
                        max_attempts=self.max_attempts,
                    )
                    await cur.execute(
                        _INSERT_POSTGRES_IF_ABSENT,
                        _record_params(first_claim.record, lease_token=token),
                    )
                    inserted_row = await cur.fetchone()
                    if inserted_row is not None:
                        inserted = _record(inserted_row)
                        return ClaimResult(
                            "claimed",
                            inserted,
                            token=token,
                            generation=inserted.generation,
                        )

                    await cur.execute(
                        f"SELECT * FROM {ASSESSMENT_TABLE} WHERE run_key = %s FOR UPDATE",
                        (key,),
                    )
                    row = await cur.fetchone()
                    # A cleanup transaction may delete a terminal row after
                    # the conflict check and before this read.  Retry the
                    # insert once so the fallback cannot regress to an
                    # unlocked read-then-upsert sequence.  Processing rows
                    # are never eligible for cleanup, so this loop normally
                    # executes at most once.
                    if row is None:
                        await cur.execute(
                            _INSERT_POSTGRES_IF_ABSENT,
                            _record_params(first_claim.record, lease_token=token),
                        )
                        inserted_row = await cur.fetchone()
                        if inserted_row is not None:
                            inserted = _record(inserted_row)
                            return ClaimResult(
                                "claimed",
                                inserted,
                                token=token,
                                generation=inserted.generation,
                            )
                        await cur.execute(
                            f"SELECT * FROM {ASSESSMENT_TABLE} WHERE run_key = %s FOR UPDATE",
                            (key,),
                        )
                        row = await cur.fetchone()
                    if row is None:
                        raise AssessmentStoreError(
                            f"PostgreSQL claim row disappeared for key {key!r}"
                        )
                    existing = _record(row) if row is not None else None
                    result = _claim_decision(
                        existing,
                        key=key,
                        ticker=ticker,
                        kind=kind,
                        profile=profile,
                        venue=venue,
                        period_at=period_at,
                        now=now,
                        expires=expires,
                        token=token,
                        max_attempts=self.max_attempts,
                    )
                    if result.token is not None or _terminalization_required(existing, result):
                        # The insert-before-lock above is the only path that
                        # creates a claim for PostgreSQL.  An existing row is
                        # locked by the SELECT, so update it in place without
                        # reintroducing a race through an upsert.
                        await cur.execute(
                            _UPDATE_POSTGRES_CLAIM,
                            _record_update_params(result.record, lease_token=result.token),
                        )
                    return result

    async def _postgres_publish(
        self,
        key: str,
        token: str,
        generation: int,
        payload: str,
        digest: str | None,
        fetched_at: str | None,
        now: datetime,
        publishable: bool,
    ) -> bool:
        async with await _postgres_connect(self.database_url) as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""
                        UPDATE {ASSESSMENT_TABLE}
                        SET status = 'completed', payload = %s, fetched_at = %s,
                            last_good_payload = COALESCE(%s, last_good_payload),
                            last_good_fetched_at = COALESCE(%s, last_good_fetched_at),
                            last_good_evidence_digest = COALESCE(%s, last_good_evidence_digest),
                            evidence_digest = %s,
                            lease_token = NULL,
                            lease_expires_at = NULL, retry_at = NULL, error = NULL,
                            updated_at = %s
                        WHERE run_key = %s AND status = 'processing'
                          AND lease_token = %s AND generation = %s
                          AND lease_expires_at > %s
                        """,
                        (
                            payload,
                            fetched_at,
                            payload if publishable else None,
                            fetched_at if publishable else None,
                            digest if publishable else None,
                            digest,
                            now,
                            key,
                            token,
                            generation,
                            now,
                        ),
                    )
                    return int(cur.rowcount) == 1

    async def _postgres_fail(
        self,
        key: str,
        token: str,
        generation: int,
        payload: str,
        error: str | None,
        digest: str | None,
        retry_at: datetime,
        now: datetime,
    ) -> bool:
        async with await _postgres_connect(self.database_url) as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""
                        UPDATE {ASSESSMENT_TABLE}
                        SET status = 'failed', payload = %s, error = %s,
                            evidence_digest = %s, fetched_at = NULL,
                            lease_token = NULL, lease_expires_at = NULL,
                            retry_at = %s, updated_at = %s
                        WHERE run_key = %s AND status = 'processing'
                          AND lease_token = %s AND generation = %s
                          AND lease_expires_at > %s
                        """,
                        (
                            payload,
                            error,
                            digest,
                            retry_at,
                            now,
                            key,
                            token,
                            generation,
                            now,
                        ),
                    )
                    return int(cur.rowcount) == 1


def _claim_decision(
    existing: AssessmentRecord | None,
    *,
    key: str,
    ticker: str,
    kind: str,
    profile: str | None,
    venue: str | None,
    period_at: datetime,
    now: datetime,
    expires: datetime,
    token: str,
    max_attempts: int,
) -> ClaimResult:
    if existing is not None:
        if existing.status is AssessmentRunStatus.completed:
            return ClaimResult("existing", existing)
        if (
            existing.status is AssessmentRunStatus.processing
            and existing.lease_expires_at is not None
            and existing.lease_expires_at > now
        ):
            return ClaimResult("in_progress", existing)
        if (
            existing.status is AssessmentRunStatus.failed
            and existing.retry_at is not None
            and existing.retry_at > now
        ):
            return ClaimResult("retry_wait", existing)
        # A worker whose lease expired is fenced at the next claim.  Counting
        # both failed attempts and expired processing leases prevents a stuck
        # upstream from creating an unbounded retry storm.
        if existing.attempts >= max_attempts:
            if existing.status is AssessmentRunStatus.processing:
                # A worker that stopped after exhausting its lease budget
                # must leave a durable terminal state.  Keeping the same
                # generation fences every late publisher while clearing the
                # lease makes the result safe to read and clean up.
                return ClaimResult(
                    "retry_exhausted",
                    replace(
                        existing,
                        status=AssessmentRunStatus.failed,
                        lease_token=None,
                        lease_expires_at=None,
                        retry_at=None,
                        error="retry_exhausted",
                    ),
                )
            # Failed rows at the retry limit are already terminal.  Return
            # them unchanged so repeated claims are idempotent and preserve
            # the original failure details and timestamps.
            return ClaimResult("retry_exhausted", existing)

    generation = (existing.generation if existing else 0) + 1
    attempts = (existing.attempts if existing else 0) + 1
    record = AssessmentRecord(
        key=key,
        ticker=ticker,
        kind=kind,
        profile=profile,
        venue=venue,
        period_at=_utc(period_at),
        status=AssessmentRunStatus.processing,
        attempts=attempts,
        generation=generation,
        lease_token=token,
        lease_expires_at=expires,
        retry_at=None,
        payload=None,
        error=None,
        evidence_digest=None,
        fetched_at=None,
        last_good_payload=(existing.last_good_payload if existing else None),
        last_good_evidence_digest=(existing.last_good_evidence_digest if existing else None),
        last_good_fetched_at=(existing.last_good_fetched_at if existing else None),
    )
    return ClaimResult("claimed", record, token=token, generation=generation)


def _terminalization_required(
    existing: AssessmentRecord | None,
    result: ClaimResult,
) -> bool:
    """Whether a claim decision needs to persist an expired-run terminal state."""

    return (
        existing is not None
        and existing.status is AssessmentRunStatus.processing
        and result.state == "retry_exhausted"
        and result.record.status is AssessmentRunStatus.failed
    )


def _is_publishable(response: AssessmentSnapshotResponse) -> bool:
    """Whether a completed payload is strong enough to become last-good.

    A snapshot may be published as the current attempt while one component is
    unavailable.  Keeping that partial attempt out of the last-good columns
    prevents a transient provider outage from erasing a previously complete
    value.  Unsupported components are acceptable when at least one applicable
    component has usable data (for example, an ETF has no equity opportunity).
    """

    if response.status is not AssessmentRunStatus.completed:
        return False
    states = tuple(response.components.values())
    if not states:
        return any(
            value is not None
            for value in (response.opportunity, response.fundamentals, response.quality)
        )
    if any(
        state.status in {AssessmentComponentStatus.failed, AssessmentComponentStatus.missing_data}
        for state in states
    ):
        return False
    return any(state.status is AssessmentComponentStatus.available for state in states)


def _record(row: Any) -> AssessmentRecord:
    payload = row["payload"] if isinstance(row, dict) else row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    last_good_payload = (
        row["last_good_payload"] if isinstance(row, dict) else row["last_good_payload"]
    )
    if isinstance(last_good_payload, str):
        last_good_payload = json.loads(last_good_payload)
    return AssessmentRecord(
        key=str(row["run_key"]),
        ticker=str(row["ticker"]),
        kind=str(row["kind"]),
        profile=row["profile"],
        venue=row["venue"],
        period_at=_parse_datetime(row["period_at"]),
        status=AssessmentRunStatus(str(row["status"])),
        attempts=int(row["attempts"]),
        generation=int(row["generation"]),
        lease_token=row["lease_token"],
        lease_expires_at=_parse_optional_datetime(row["lease_expires_at"]),
        retry_at=_parse_optional_datetime(row["retry_at"]),
        payload=payload,
        error=row["error"],
        evidence_digest=row["evidence_digest"],
        fetched_at=_parse_optional_datetime(row["fetched_at"]),
        last_good_payload=last_good_payload,
        last_good_evidence_digest=row["last_good_evidence_digest"],
        last_good_fetched_at=_parse_optional_datetime(row["last_good_fetched_at"]),
    )


def _record_params(record: AssessmentRecord, *, lease_token: str | None) -> tuple[Any, ...]:
    return (
        record.key,
        record.ticker,
        record.kind,
        record.profile,
        record.venue,
        record.period_at.isoformat(),
        record.status.value,
        record.attempts,
        record.generation,
        lease_token,
        record.lease_expires_at.isoformat() if record.lease_expires_at else None,
        record.retry_at.isoformat() if record.retry_at else None,
        json.dumps(record.payload, separators=(",", ":")) if record.payload else None,
        record.error,
        record.evidence_digest,
        record.fetched_at.isoformat() if record.fetched_at else None,
        json.dumps(record.last_good_payload, separators=(",", ":"))
        if record.last_good_payload
        else None,
        record.last_good_evidence_digest,
        record.last_good_fetched_at.isoformat() if record.last_good_fetched_at else None,
        datetime.now(UTC).isoformat(),
    )


def _record_update_params(
    record: AssessmentRecord,
    *,
    lease_token: str | None,
) -> tuple[Any, ...]:
    """Order record values for the PostgreSQL in-place claim update."""

    params = _record_params(record, lease_token=lease_token)
    return (*params[1:], params[0])


def _parse_datetime(value: Any) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return _utc(parsed)


def _parse_optional_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    return _parse_datetime(value)


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _assessment_period_closed(period_at: datetime, now: datetime) -> bool:
    """Return whether a scheduler slot's capture window has ended."""

    local_period = period_at.astimezone(ASSESSMENT_TIMEZONE)
    local_now = now.astimezone(ASSESSMENT_TIMEZONE)
    if local_now < local_period:
        return False
    next_hour = {12: 14, 14: 16, 16: 19, 19: 0}.get(local_period.hour)
    if next_hour is None:
        # Durable rows created before scheduler-slot validation are safe to
        # terminalize once their lease expires; they cannot be reclaimed as a
        # valid current-period calculation.
        return True
    start = local_period.replace(minute=0, second=0, microsecond=0)
    end = (
        (start + timedelta(days=1)).replace(hour=0)
        if next_hour == 0
        else start.replace(hour=next_hour)
    )
    return local_now >= end


_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {ASSESSMENT_TABLE} (
    run_key TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    kind TEXT NOT NULL,
    profile TEXT,
    venue TEXT,
    period_at TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    generation INTEGER NOT NULL DEFAULT 0,
    lease_token TEXT,
    lease_expires_at TEXT,
    retry_at TEXT,
    payload TEXT,
    error TEXT,
    evidence_digest TEXT,
    fetched_at TEXT,
    last_good_payload TEXT,
    last_good_evidence_digest TEXT,
    last_good_fetched_at TEXT,
    updated_at TEXT NOT NULL
);
"""

_POSTGRES_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {ASSESSMENT_TABLE} (
    run_key TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    kind TEXT NOT NULL,
    profile TEXT,
    venue TEXT,
    period_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    generation INTEGER NOT NULL DEFAULT 0,
    lease_token TEXT,
    lease_expires_at TIMESTAMPTZ,
    retry_at TIMESTAMPTZ,
    payload TEXT,
    error TEXT,
    evidence_digest TEXT,
    fetched_at TIMESTAMPTZ,
    last_good_payload TEXT,
    last_good_evidence_digest TEXT,
    last_good_fetched_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL
);
"""

_POSTGRES_MIGRATIONS = (
    f"ALTER TABLE {ASSESSMENT_TABLE} ADD COLUMN IF NOT EXISTS venue TEXT",
    f"ALTER TABLE {ASSESSMENT_TABLE} ADD COLUMN IF NOT EXISTS last_good_payload TEXT",
    f"ALTER TABLE {ASSESSMENT_TABLE} ADD COLUMN IF NOT EXISTS last_good_evidence_digest TEXT",
    f"ALTER TABLE {ASSESSMENT_TABLE} ADD COLUMN IF NOT EXISTS last_good_fetched_at TIMESTAMPTZ",
)
_POSTGRES_INDEX = f"""
CREATE INDEX IF NOT EXISTS ix_fundamentus_assessment_snapshots_period
    ON {ASSESSMENT_TABLE} (ticker, kind, profile, venue, period_at)
"""

# ``CREATE TABLE IF NOT EXISTS`` does not add columns to an installation that
# predates venue-aware identity or last-good retention.  These additive SQLite
# migrations run at startup and deliberately never touch the backend's own
# ``assessment_runs`` table.
_SQLITE_MIGRATIONS = (
    f"ALTER TABLE {ASSESSMENT_TABLE} ADD COLUMN venue TEXT",
    f"ALTER TABLE {ASSESSMENT_TABLE} ADD COLUMN last_good_payload TEXT",
    f"ALTER TABLE {ASSESSMENT_TABLE} ADD COLUMN last_good_evidence_digest TEXT",
    f"ALTER TABLE {ASSESSMENT_TABLE} ADD COLUMN last_good_fetched_at TEXT",
)
_SQLITE_INDEX = f"""
CREATE INDEX IF NOT EXISTS ix_fundamentus_assessment_snapshots_period
    ON {ASSESSMENT_TABLE} (ticker, kind, profile, venue, period_at)
"""

_UPSERT_SQLITE = f"""
INSERT INTO {ASSESSMENT_TABLE} (
    run_key, ticker, kind, profile, venue, period_at, status, attempts, generation,
    lease_token, lease_expires_at, retry_at, payload, error, evidence_digest,
    fetched_at, last_good_payload, last_good_evidence_digest,
    last_good_fetched_at, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(run_key) DO UPDATE SET
    ticker = excluded.ticker,
    kind = excluded.kind,
    profile = excluded.profile,
    venue = excluded.venue,
    period_at = excluded.period_at,
    status = excluded.status,
    attempts = excluded.attempts,
    generation = excluded.generation,
    lease_token = excluded.lease_token,
    lease_expires_at = excluded.lease_expires_at,
    retry_at = excluded.retry_at,
    payload = excluded.payload,
    error = excluded.error,
    evidence_digest = excluded.evidence_digest,
    fetched_at = excluded.fetched_at,
    last_good_payload = excluded.last_good_payload,
    last_good_evidence_digest = excluded.last_good_evidence_digest,
    last_good_fetched_at = excluded.last_good_fetched_at,
    updated_at = excluded.updated_at
"""

_UPSERT_POSTGRES = _UPSERT_SQLITE.replace("?", "%s")

# PostgreSQL claims use an insert-before-lock protocol.  The unique primary
# key serializes first claims for a key even when no row existed at the start
# of either transaction; ``RETURNING`` lets the winner use the row it inserted
# without another read.
_INSERT_POSTGRES_IF_ABSENT = f"""
INSERT INTO {ASSESSMENT_TABLE} (
    run_key, ticker, kind, profile, venue, period_at, status, attempts, generation,
    lease_token, lease_expires_at, retry_at, payload, error, evidence_digest,
    fetched_at, last_good_payload, last_good_evidence_digest,
    last_good_fetched_at, updated_at
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (run_key) DO NOTHING
RETURNING *
"""

_UPDATE_POSTGRES_CLAIM = f"""
UPDATE {ASSESSMENT_TABLE}
SET ticker = %s, kind = %s, profile = %s, venue = %s,
    period_at = %s, status = %s, attempts = %s, generation = %s,
    lease_token = %s, lease_expires_at = %s, retry_at = %s,
    payload = %s, error = %s, evidence_digest = %s, fetched_at = %s,
    last_good_payload = %s, last_good_evidence_digest = %s,
    last_good_fetched_at = %s, updated_at = %s
WHERE run_key = %s
"""


__all__ = [
    "ASSESSMENT_TABLE",
    "ASSESSMENT_MAINTENANCE_BATCH_SIZE",
    "MAX_LEASE_SECONDS",
    "AssessmentClaim",
    "AssessmentRecord",
    "AssessmentStoreError",
    "AssessmentStore",
    "ClaimResult",
]

# Compatibility alias used by callers that want to name the claim explicitly.
AssessmentClaim = ClaimResult
