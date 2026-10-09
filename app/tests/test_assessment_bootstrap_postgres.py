from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.assessment import store as assessment_store
from app.assessment.models import AssessmentRunStatus
from app.assessment.store import (
    ASSESSMENT_BOOTSTRAP_TABLE,
    ASSESSMENT_TABLE,
    AssessmentStore,
    AssessmentStoreError,
    BootstrapIdentityConflictError,
    BootstrapWork,
    _bootstrap_work_params,
)

_QUEUE_COLUMNS = (
    "run_key",
    "bootstrap_key",
    "ticker",
    "kind",
    "venue",
    "corporate_name",
    "period_at",
    "phase",
    "lease_token",
    "lease_expires_at",
    "retry_at",
    "error",
    "created_at",
    "updated_at",
)
_SNAPSHOT_COLUMNS = (
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


class _PGTransaction:
    def __init__(self, connection: _PGConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _PGTransaction:
        return self

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        return None


class _PGCursor:
    def __init__(self, connection: _PGConnection) -> None:
        self.connection = connection
        self.pending: dict[str, Any] | None = None
        self.pending_rows: list[dict[str, Any]] = []
        self.rowcount = 0

    async def __aenter__(self) -> _PGCursor:
        return self

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        return None

    async def execute(self, query: str, params: tuple[Any, ...] = ()) -> None:
        normalized = " ".join(query.upper().split())
        queue_name = ASSESSMENT_BOOTSTRAP_TABLE.upper()
        snapshot_name = ASSESSMENT_TABLE.upper()
        self.pending = None
        self.pending_rows = []
        self.rowcount = 0

        if normalized.startswith("INSERT INTO ") and queue_name in normalized:
            values = dict(zip(_QUEUE_COLUMNS, params, strict=True))
            self.connection.database.queue.setdefault(str(values["run_key"]), values)
            return

        if normalized.startswith("SELECT * FROM ") and f"FROM {snapshot_name}" in normalized:
            if "FOR UPDATE" in normalized:
                self.pending_rows = [
                    dict(row)
                    for row in self.connection.database.snapshots.values()
                    if row["status"] == AssessmentRunStatus.processing.value
                ]
            else:
                key = str(params[0])
                row = self.connection.database.snapshots.get(key)
                self.pending = dict(row) if row is not None else None
            return

        if normalized.startswith("SELECT * FROM ") and f"FROM {queue_name}" in normalized:
            if "WHERE PHASE = 'QUEUED'" in normalized:
                self.pending_rows = [dict(row) for row in self.connection.database.queue.values()]
            else:
                key = str(params[0])
                row = self.connection.database.queue.get(key)
                self.pending = dict(row) if row is not None else None
            return

        if normalized.startswith("SELECT 1 FROM ") and queue_name in normalized:
            self.pending = {"?column?": 1} if self.connection.database.queue_probe else None
            return

        if normalized.startswith("UPDATE ") and queue_name in normalized:
            if "SET BOOTSTRAP_KEY" in normalized:
                key = str(params[-1])
            elif "SET LEASE_EXPIRES_AT" in normalized:
                key = str(params[2])
            else:
                key = str(params[-2])
            row = self.connection.database.queue.get(key)
            if row is None:
                return
            if "SET BOOTSTRAP_KEY" in normalized:
                for column, value in zip(_QUEUE_COLUMNS[1:], params[:-1], strict=True):
                    row[column] = value
            elif "SET LEASE_EXPIRES_AT" in normalized:
                row["lease_expires_at"], row["updated_at"] = params[:2]
            elif "SET PHASE = %S" in normalized:
                row["phase"] = params[0]
                row["lease_token"] = None
                row["lease_expires_at"] = None
                row["retry_at"], row["error"], row["updated_at"] = params[1:4]
            else:
                row["phase"] = "queued"
                row["lease_token"] = None
                row["lease_expires_at"] = None
                row["retry_at"], row["updated_at"] = None, params[0]
            self.rowcount = 1
            return

        if normalized.startswith("UPDATE ") and snapshot_name in normalized:
            key = str(
                params[1]
                if "SET STATUS = 'FAILED'" in normalized and "LEASE_EXPIRES_AT <=" in normalized
                else params[2]
            )
            row = self.connection.database.snapshots.get(key)
            if row is None:
                return
            if "SET STATUS = 'FAILED'" in normalized:
                row["status"] = AssessmentRunStatus.failed.value
                row["error"] = (
                    "lease_expired" if "LEASE_EXPIRES_AT <=" in normalized else "cancelled"
                )
                row["lease_token"] = None
                row["lease_expires_at"] = None
                row["retry_at"], row["updated_at"] = params[:2]
            else:
                row["lease_expires_at"], row["updated_at"] = params[:2]
            self.rowcount = 1
            return

        if normalized.startswith("DELETE FROM ") and snapshot_name in normalized:
            self.rowcount = self.connection.database.cleanup_snapshot_count
            return
        if normalized.startswith("DELETE FROM ") and queue_name in normalized:
            self.rowcount = self.connection.database.cleanup_queue_count
            return

        raise AssertionError(f"unexpected PostgreSQL statement: {query}")

    async def fetchone(self) -> dict[str, Any] | None:
        if self.pending is not None:
            row = self.pending
            self.pending = None
            return row
        return None

    async def fetchall(self) -> list[dict[str, Any]]:
        rows = self.pending_rows
        self.pending_rows = []
        return rows


class _PGConnection:
    def __init__(self, database: _PGDatabase) -> None:
        self.database = database

    async def __aenter__(self) -> _PGConnection:
        return self

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        return None

    def transaction(self) -> _PGTransaction:
        return _PGTransaction(self)

    def cursor(self, *, row_factory: object | None = None) -> _PGCursor:
        del row_factory
        return _PGCursor(self)

    async def execute(self, _query: str, _params: tuple[Any, ...] = ()) -> None:
        return None

    async def commit(self) -> None:
        return None


class _PGDatabase:
    def __init__(self) -> None:
        self.queue: dict[str, dict[str, Any]] = {}
        self.snapshots: dict[str, dict[str, Any]] = {}
        self.queue_probe = True
        self.cleanup_snapshot_count = 0
        self.cleanup_queue_count = 0


async def _store_with_fake_postgres(
    monkeypatch: pytest.MonkeyPatch,
    database: _PGDatabase,
) -> AssessmentStore:
    async def connect(_url: str | None) -> _PGConnection:
        return _PGConnection(database)

    monkeypatch.setattr(assessment_store, "_postgres_connect", connect)
    store = AssessmentStore(database_url="postgresql://bootstrap/test", default_lease_seconds=30)
    await store.startup()
    return store


def _snapshot_row(key: str, now: datetime, *, period_at: datetime | None = None) -> dict[str, Any]:
    return dict(
        zip(
            _SNAPSHOT_COLUMNS,
            (
                key,
                "TEST3",
                "stock",
                None,
                "BVMF",
                period_at or now,
                AssessmentRunStatus.processing.value,
                1,
                1,
                "snapshot-token",
                now + timedelta(minutes=5),
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                now,
            ),
            strict=True,
        )
    )


def _work(key: str, now: datetime, *, phase: str = "queued") -> BootstrapWork:
    return BootstrapWork(
        key=key,
        bootstrap_key="b" * 64,
        ticker="TEST3",
        kind="stock",
        venue="BVMF",
        corporate_name="Issuer",
        period_at=now,
        phase=phase,
        lease_token="queue-token" if phase == "running" else None,
        lease_expires_at=now + timedelta(minutes=5) if phase == "running" else None,
        retry_at=None,
        error=None,
        created_at=now - timedelta(minutes=1),
        updated_at=now - timedelta(minutes=1),
    )


def _insert_queue(database: _PGDatabase, work: BootstrapWork) -> None:
    database.queue[work.key] = dict(zip(_QUEUE_COLUMNS, _bootstrap_work_params(work), strict=True))


@pytest.mark.asyncio
async def test_postgres_bootstrap_queue_sql_and_fences(monkeypatch: pytest.MonkeyPatch) -> None:
    database = _PGDatabase()
    store = await _store_with_fake_postgres(monkeypatch, database)
    now = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)
    key = "assessment:7:bootstrap:TEST3:stock:BVMF:" + "b" * 64
    try:
        admitted = await store.enqueue_bootstrap(
            key=key,
            bootstrap_key="b" * 64,
            ticker="TEST3",
            kind="stock",
            venue="BVMF",
            corporate_name="Issuer",
            period_at=now,
            now=now,
        )
        assert admitted.state == "processing"
        claimed = await store.claim_bootstrap_due(limit=1, now=now, lease_seconds=30)
        assert len(claimed) == 1
        claim = claimed[0]
        assert await store.renew_bootstrap_lease(
            key=key, token=claim.token, now=now, lease_seconds=30
        )
        assert await store.complete_bootstrap(
            key=key, token=claim.token, success=False, retry_after_seconds=30, error="temporary"
        )
        retry = await store.enqueue_bootstrap(
            key=key,
            bootstrap_key="b" * 64,
            ticker="TEST3",
            kind="stock",
            venue="BVMF",
            corporate_name="Later",
            period_at=now,
            now=now,
        )
        assert retry.state == "retry_wait"
        database.queue[key]["phase"] = "running"
        database.queue[key]["lease_token"] = claim.token
        database.queue[key]["lease_expires_at"] = now + timedelta(minutes=5)
        assert await store.release_bootstrap(key=key, token=claim.token, now=now)
        with pytest.raises(BootstrapIdentityConflictError):
            await store.enqueue_bootstrap(
                key=key,
                bootstrap_key="b" * 64,
                ticker="TEST3",
                kind="stock",
                venue="BVMF",
                period_at=now + timedelta(seconds=1),
                now=now,
            )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_postgres_bootstrap_missing_rows_and_claim_lease_ops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _PGDatabase()
    store = await _store_with_fake_postgres(monkeypatch, database)
    now = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)
    key = "assessment:7:bootstrap:TEST3:stock:BVMF:" + "c" * 64
    try:
        database.queue_probe = False
        assert await store.cleanup_before(now, batch_size=1) == 0
        database.queue_probe = True
        database.cleanup_snapshot_count = 0
        database.cleanup_queue_count = 1
        old = _work(key, now - timedelta(days=2))
        old = old.__class__(**{**old.__dict__, "phase": "done"})
        _insert_queue(database, old)
        assert await store.cleanup_before(now, batch_size=1) == 1
        database.cleanup_snapshot_count = 1
        assert await store.cleanup_before(now, batch_size=1) == 1

        queue = _work(key, now, phase="running")
        _insert_queue(database, queue)
        database.snapshots[key] = _snapshot_row(key, now, period_at=now - timedelta(days=2))
        assert await store.renew_claim(
            key=key, token="snapshot-token", generation=1, now=now, lease_seconds=30
        )
        assert await store.release_claim(key=key, token="snapshot-token", generation=1, now=now)
        assert database.snapshots[key]["status"] == AssessmentRunStatus.failed.value
        assert await store.release_bootstrap(key=key, token="queue-token", now=now) is True
        database.queue[key]["phase"] = "running"
        database.queue[key]["lease_token"] = "queue-token"
        database.queue[key]["lease_expires_at"] = now + timedelta(minutes=5)
        assert await store.complete_bootstrap(key=key, token="queue-token", success=True, now=now)

        database.queue[key]["phase"] = "failed"
        database.queue[key]["retry_at"] = (now - timedelta(seconds=1)).isoformat()
        database.queue[key]["error"] = "temporary"
        refreshed = await store.enqueue_bootstrap(
            key=key,
            bootstrap_key="b" * 64,
            ticker="TEST3",
            kind="stock",
            venue="BVMF",
            corporate_name="Issuer",
            period_at=now,
            now=now,
        )
        assert refreshed.state == "queued"

        database.snapshots[key] = _snapshot_row(key, now)
        database.snapshots[key]["status"] = AssessmentRunStatus.completed.value
        completed = await store.enqueue_bootstrap(
            key=key,
            bootstrap_key="b" * 64,
            ticker="TEST3",
            kind="stock",
            venue="BVMF",
            corporate_name="Issuer",
            period_at=now,
            now=now,
        )
        assert completed.state == "completed"

        database.snapshots[key]["status"] = AssessmentRunStatus.failed.value
        database.snapshots[key]["retry_at"] = (now + timedelta(seconds=10)).isoformat()
        waiting = await store.enqueue_bootstrap(
            key=key,
            bootstrap_key="b" * 64,
            ticker="TEST3",
            kind="stock",
            venue="BVMF",
            corporate_name="Issuer",
            period_at=now,
            now=now,
        )
        assert waiting.state == "retry_wait"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_postgres_terminalize_and_bootstrap_disappeared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _PGDatabase()
    store = await _store_with_fake_postgres(monkeypatch, database)
    now = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)
    key = "assessment:7:bootstrap:TEST3:stock:BVMF:" + "d" * 64
    try:
        database.snapshots["scheduled"] = _snapshot_row(
            "scheduled", now - timedelta(minutes=1), period_at=now - timedelta(days=2)
        )
        database.snapshots["scheduled"]["lease_expires_at"] = now - timedelta(minutes=1)
        assert await store.terminalize_expired(now=now, batch_size=2) == 1

        database.queue_probe = True
        database.queue.clear()
        # A stale queue admission that disappears between INSERT and SELECT
        # must fail closed rather than acknowledge work that cannot recover.
        original = database.queue
        database.queue = _DisappearingQueue()
        with pytest.raises(AssessmentStoreError, match="row disappeared"):
            await store.enqueue_bootstrap(
                key=key,
                bootstrap_key="d" * 64,
                ticker="TEST3",
                kind="stock",
                venue="BVMF",
                period_at=now,
                now=now,
            )
        database.queue = original
    finally:
        await store.close()


class _DisappearingQueue(dict[str, dict[str, Any]]):
    def setdefault(self, _key: str, _value: dict[str, Any]) -> None:  # type: ignore[override]
        return None
