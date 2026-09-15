from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.assessment import store as assessment_store
from app.assessment.models import AssessmentRunStatus
from app.assessment.store import ASSESSMENT_TABLE, AssessmentStore, AssessmentStoreError

_COLUMNS = (
    "run_key",
    "ticker",
    "kind",
    "profile",
    "venue",
    "period_at",
    "status",
    "attempts",
    "generation",
    "lease_token",
    "lease_expires_at",
    "retry_at",
    "payload",
    "error",
    "evidence_digest",
    "fetched_at",
    "last_good_payload",
    "last_good_evidence_digest",
    "last_good_fetched_at",
    "updated_at",
)


class _Transaction:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _Transaction:
        return self

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.connection.release_claim_lock()


class _Cursor:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection
        self.pending_row: dict[str, Any] | None = None

    async def __aenter__(self) -> _Cursor:
        return self

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        return None

    async def execute(self, query: str, params: tuple[Any, ...] = ()) -> None:
        normalized = query.lstrip().upper()
        if normalized.startswith("INSERT") and "ON CONFLICT (RUN_KEY) DO NOTHING" in normalized:
            self.connection.database.insert_attempts += 1
            await self.connection.acquire_claim_lock()
            key = str(params[0])
            if (
                self.connection.database.force_insert_conflict
                or key in self.connection.database.rows
            ):
                self.pending_row = None
            else:
                inserted_row = dict(zip(_COLUMNS, params, strict=True))
                self.connection.database.rows[key] = inserted_row
                self.pending_row = inserted_row.copy()
            return
        if normalized.startswith("SELECT") and "FOR UPDATE" in normalized:
            self.connection.database.select_for_update_attempts += 1
            await self.connection.acquire_claim_lock()
            key = str(params[0])
            selected_row = self.connection.database.rows.get(key)
            if self.connection.database.force_select_missing:
                selected_row = None
            self.pending_row = selected_row.copy() if selected_row is not None else None
            return
        if normalized.startswith("UPDATE") and f"UPDATE {ASSESSMENT_TABLE.upper()}" in normalized:
            self.connection.database.update_attempts += 1
            values = params
            key = str(values[-1])
            row = self.connection.database.rows[key]
            for column, value in zip(_COLUMNS[1:], values[:-1], strict=True):
                row[column] = value
            self.pending_row = None
            return
        raise AssertionError(f"unexpected PostgreSQL statement: {query}")

    async def fetchone(self) -> dict[str, Any] | None:
        row = self.pending_row
        self.pending_row = None
        return row


class _Connection:
    def __init__(self, database: _Database) -> None:
        self.database = database
        self.claim_lock_held = False
        self.setup_statements: list[str] = []

    async def __aenter__(self) -> _Connection:
        return self

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.release_claim_lock()

    def transaction(self) -> _Transaction:
        return _Transaction(self)

    def cursor(self, *, row_factory: object | None = None) -> _Cursor:
        del row_factory
        return _Cursor(self)

    async def execute(self, query: str, _params: tuple[Any, ...] = ()) -> None:
        self.setup_statements.append(query)

    async def commit(self) -> None:
        return None

    async def acquire_claim_lock(self) -> None:
        if not self.claim_lock_held:
            await self.database.claim_lock.acquire()
            self.claim_lock_held = True
            # Ensure a second transaction reaches the unique-key conflict
            # while this transaction still owns the inserted row lock.
            await asyncio.sleep(0)

    def release_claim_lock(self) -> None:
        if self.claim_lock_held:
            self.claim_lock_held = False
            self.database.claim_lock.release()


class _Database:
    def __init__(self) -> None:
        self.claim_lock = asyncio.Lock()
        self.rows: dict[str, dict[str, Any]] = {}
        self.insert_attempts = 0
        self.select_for_update_attempts = 0
        self.update_attempts = 0
        self.force_insert_conflict = False
        self.force_select_missing = False


def _row(
    *,
    key: str,
    now: datetime,
    status: str = AssessmentRunStatus.processing,
    attempts: int = 1,
    generation: int = 1,
    lease_token: str | None = "existing-token",
    lease_expires_at: datetime | None = None,
) -> dict[str, Any]:
    expires = lease_expires_at or now + timedelta(minutes=10)
    return {
        "run_key": key,
        "ticker": "TEST3",
        "kind": "stock",
        "profile": None,
        "venue": "BVMF",
        "period_at": now,
        "status": status,
        "attempts": attempts,
        "generation": generation,
        "lease_token": lease_token,
        "lease_expires_at": expires,
        "retry_at": None,
        "payload": None,
        "error": None,
        "evidence_digest": None,
        "fetched_at": None,
        "last_good_payload": None,
        "last_good_evidence_digest": None,
        "last_good_fetched_at": None,
        "updated_at": now,
    }


async def _store_with_fake_postgres(
    monkeypatch: pytest.MonkeyPatch,
    database: _Database,
    *,
    max_attempts: int = 3,
) -> AssessmentStore:
    async def connect(_database_url: str | None) -> _Connection:
        return _Connection(database)

    monkeypatch.setattr(assessment_store, "_postgres_connect", connect)
    store = AssessmentStore(
        database_url="postgresql://user:pass@db/test",
        max_attempts=max_attempts,
    )
    await store.startup()
    return store


@pytest.mark.asyncio
async def test_postgres_setup_applies_migrations_before_venue_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _Database()
    connection = _Connection(database)

    async def connect(_database_url: str | None) -> _Connection:
        return connection

    monkeypatch.setattr(assessment_store, "_postgres_connect", connect)
    store = AssessmentStore(database_url="postgresql://user:pass@db/test")
    await store.startup()
    await store.close()

    index_position = next(
        index
        for index, statement in enumerate(connection.setup_statements)
        if "CREATE INDEX IF NOT EXISTS" in statement
    )
    migration_positions = [
        index
        for index, statement in enumerate(connection.setup_statements)
        if "ALTER TABLE" in statement
    ]
    assert migration_positions
    assert index_position > max(migration_positions)


@pytest.mark.asyncio
async def test_postgres_new_key_claim_has_one_owner_under_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _Database()
    store = await _store_with_fake_postgres(monkeypatch, database)
    now = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)
    try:
        first, second = await asyncio.gather(
            store.claim(
                key="shared-key",
                ticker="TEST3",
                kind="stock",
                profile=None,
                venue="BVMF",
                period_at=now,
                now=now,
            ),
            store.claim(
                key="shared-key",
                ticker="TEST3",
                kind="stock",
                profile=None,
                venue="BVMF",
                period_at=now,
                now=now,
            ),
        )
    finally:
        await store.close()

    assert {first.state, second.state} == {"claimed", "in_progress"}
    owner = first if first.state == "claimed" else second
    observer = second if owner is first else first
    assert owner.token is not None and owner.generation == 1
    assert observer.token is None and observer.generation is None
    assert owner.record.lease_token == owner.token
    assert owner.record.attempts == observer.record.attempts == 1
    assert database.insert_attempts == 2
    assert database.select_for_update_attempts == 1
    assert database.update_attempts == 0


@pytest.mark.asyncio
async def test_postgres_existing_expired_claim_is_locked_before_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _Database()
    now = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)
    database.rows["expired-key"] = _row(
        key="expired-key",
        now=now - timedelta(minutes=20),
        lease_expires_at=now - timedelta(minutes=1),
    )
    store = await _store_with_fake_postgres(monkeypatch, database)
    try:
        result = await store.claim(
            key="expired-key",
            ticker="TEST3",
            kind="stock",
            profile=None,
            venue="BVMF",
            period_at=now,
            now=now,
        )
    finally:
        await store.close()

    assert result.state == "claimed"
    assert result.generation == 2
    assert result.record.attempts == 2
    assert result.token is not None
    assert database.rows["expired-key"]["lease_token"] == result.token
    assert database.insert_attempts == 1
    assert database.select_for_update_attempts == 1
    assert database.update_attempts == 1


@pytest.mark.asyncio
async def test_postgres_exhausted_expired_claim_is_terminalized_and_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _Database()
    now = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)
    database.rows["exhausted-key"] = _row(
        key="exhausted-key",
        now=now - timedelta(minutes=20),
        attempts=2,
        generation=2,
        lease_token="stale-token",
        lease_expires_at=now - timedelta(minutes=1),
    )
    store = await _store_with_fake_postgres(monkeypatch, database, max_attempts=2)
    try:
        result = await store.claim(
            key="exhausted-key",
            ticker="TEST3",
            kind="stock",
            profile=None,
            venue="BVMF",
            period_at=now,
            now=now,
        )
        repeated = await store.claim(
            key="exhausted-key",
            ticker="TEST3",
            kind="stock",
            profile=None,
            venue="BVMF",
            period_at=now,
            now=now + timedelta(minutes=1),
        )
    finally:
        await store.close()

    assert result.state == "retry_exhausted"
    assert result.token is None
    assert result.record.status is AssessmentRunStatus.failed
    assert result.record.error == "retry_exhausted"
    persisted = database.rows["exhausted-key"]
    assert persisted["status"] == AssessmentRunStatus.failed
    assert persisted["error"] == "retry_exhausted"
    assert persisted["lease_token"] is None
    assert persisted["lease_expires_at"] is None
    assert persisted["retry_at"] is None
    assert repeated.state == "retry_exhausted"
    assert repeated.record.error == "retry_exhausted"
    assert database.update_attempts == 1


@pytest.mark.asyncio
async def test_postgres_claim_fails_closed_when_conflicting_row_keeps_disappearing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _Database()
    database.force_insert_conflict = True
    database.force_select_missing = True
    store = await _store_with_fake_postgres(monkeypatch, database)
    now = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)
    try:
        with pytest.raises(AssessmentStoreError, match="claim row disappeared"):
            await store.claim(
                key="disappearing-key",
                ticker="TEST3",
                kind="stock",
                profile=None,
                venue="BVMF",
                period_at=now,
                now=now,
            )
    finally:
        await store.close()

    assert database.insert_attempts == 2
    assert database.select_for_update_attempts == 2
    assert database.update_attempts == 0
