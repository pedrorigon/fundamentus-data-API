from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest
from fastapi import Response
from pydantic import ValidationError

import app.assessment.service as assessment_service
from app.api.routes import (
    _resolve_fundamentals,
    resolve_fundamentals_batch,
)
from app.assessment.models import (
    ASSESSMENT_TIMEZONE,
    AssessmentComponentState,
    AssessmentComponentStatus,
    AssessmentRunStatus,
    AssessmentSnapshotRequest,
    AssessmentSnapshotResponse,
    response_payload,
)
from app.assessment.routes import get_assessment_service, resolve_assessment_snapshot
from app.assessment.service import (
    AssessmentSnapshotService,
    InvalidAssessmentPeriodError,
    _assessment_window_state,
    _error_state,
    _overall_status,
    _remaining_seconds,
    _trusted_b3_name,
    assessment_key,
)
from app.assessment.store import (
    ASSESSMENT_TABLE,
    AssessmentRecord,
    AssessmentStore,
    _is_publishable,
    _normalize_database_url,
    _postgres_connect,
    _postgres_row_factory,
)
from app.core.errors import (
    InvalidTickerError,
    ProviderUnavailableError,
    UpstreamUnavailableError,
)
from app.models import (
    FinancialPeriod,
    FundamentalsBatchRequest,
    FundamentalsSnapshot,
    InstrumentMetadata,
    InstrumentType,
    OpportunityMetric,
    OpportunityMetrics,
    OpportunityResponse,
    QualityAssetFacts,
    QualityFactsRequest,
    QualityFactsResponse,
)
from app.models.quality import QualityAssetKind, QualityFact, QualityFactObservation


def _response(period: datetime, *, ticker: str = "TEST3") -> AssessmentSnapshotResponse:
    return AssessmentSnapshotResponse(
        ticker=ticker,
        kind=QualityAssetKind.stock,
        period_at=period,
        status=AssessmentRunStatus.completed,
        evidence_digest="digest",
    )


def _period() -> datetime:
    local = datetime.now(ASSESSMENT_TIMEZONE).replace(
        hour=12,
        minute=0,
        second=0,
        microsecond=0,
    )
    return local.astimezone(UTC)


@pytest.fixture(autouse=True)
def _freeze_assessment_window_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep service integration tests inside today's 12:00 capture window."""

    monkeypatch.setattr(
        assessment_service,
        "_now_local",
        lambda: datetime.now(ASSESSMENT_TIMEZONE).replace(
            hour=12,
            minute=30,
            second=0,
            microsecond=0,
        ),
    )


def _opportunity(ticker: str, kind: InstrumentType = InstrumentType.stock) -> OpportunityResponse:
    metric = OpportunityMetric(value="10", sources=["fundamentus"])
    metrics = OpportunityMetrics(
        current_price=metric,
        shares_outstanding=metric,
        earnings_per_share=metric,
        book_value_per_share=metric,
        price_to_book=metric,
        price_to_earnings=metric,
        dividend_yield_12m=metric,
        dividends_12m=metric,
        graham_price=metric,
        bazin_price=metric,
        min_52_weeks=metric,
        max_52_weeks=metric,
    )
    return OpportunityResponse(
        ticker=ticker,
        instrument=InstrumentMetadata(ticker=ticker, instrument_type=kind),
        metrics=metrics,
        refreshed_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run_status", "http_status"),
    [
        (AssessmentRunStatus.completed, 200),
        (AssessmentRunStatus.processing, 202),
        (AssessmentRunStatus.failed, 503),
    ],
)
async def test_assessment_route_maps_run_status_to_http_contract(
    run_status: AssessmentRunStatus,
    http_status: int,
) -> None:
    class StubService:
        async def resolve(self, _payload: AssessmentSnapshotRequest) -> AssessmentSnapshotResponse:
            return _response(_period()).model_copy(
                update={"status": run_status, "retry_after_seconds": 7200}
            )

    response = Response()
    result = await resolve_assessment_snapshot(
        AssessmentSnapshotRequest(
            ticker="TEST3",
            kind=QualityAssetKind.stock,
            period_at=_period(),
        ),
        StubService(),  # type: ignore[arg-type]
        response,
    )

    assert result.status is run_status
    assert response.status_code == http_status
    assert response.headers["Retry-After"] == "3600"


@pytest.mark.asyncio
async def test_claim_is_idempotent_and_publish_is_fenced_by_lease(tmp_path: Path) -> None:
    now = datetime(2026, 9, 13, 15, 0, tzinfo=UTC)
    period = datetime(2026, 9, 12, 15, 0, tzinfo=UTC)
    store = AssessmentStore(tmp_path / "assessment.sqlite3", default_lease_seconds=10)
    await store.startup()
    try:
        first = await store.claim(
            key="key",
            ticker="TEST3",
            kind="stock",
            profile=None,
            period_at=period,
            now=now,
        )
        second = await store.claim(
            key="key",
            ticker="TEST3",
            kind="stock",
            profile=None,
            period_at=period,
            now=now + timedelta(seconds=1),
        )
        assert first.state == "claimed"
        assert second.state == "in_progress"
        assert first.token is not None and first.generation is not None
        assert not await store.publish(
            key="key",
            token=first.token,
            generation=first.generation,
            response=_response(period),
            now=now + timedelta(seconds=11),
        )
        assert await store.publish(
            key="key",
            token=first.token,
            generation=first.generation,
            response=_response(period),
            now=now + timedelta(seconds=5),
        )
        assert (await store.get("key")).status is AssessmentRunStatus.completed  # type: ignore[union-attr]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_expired_leases_are_reclaimed_only_within_retry_budget(tmp_path: Path) -> None:
    now = datetime(2026, 9, 13, 15, 0, tzinfo=UTC)
    period = datetime(2026, 9, 12, 15, 0, tzinfo=UTC)
    store = AssessmentStore(
        tmp_path / "assessment.sqlite3",
        default_lease_seconds=1,
        max_attempts=2,
    )
    await store.startup()
    try:
        first = await store.claim(
            key="key",
            ticker="TEST3",
            kind="stock",
            profile=None,
            period_at=period,
            now=now,
        )
        second = await store.claim(
            key="key",
            ticker="TEST3",
            kind="stock",
            profile=None,
            period_at=period,
            now=now + timedelta(seconds=2),
        )
        third = await store.claim(
            key="key",
            ticker="TEST3",
            kind="stock",
            profile=None,
            period_at=period,
            now=now + timedelta(seconds=4),
        )
        assert first.state == "claimed"
        assert second.state == "claimed"
        assert second.record.generation == 2
        assert third.state == "retry_exhausted"
        assert third.token is None
        persisted = await store.get("key")
        assert persisted is not None
        assert persisted.status is AssessmentRunStatus.failed
        assert persisted.error == "retry_exhausted"
        assert persisted.lease_token is None
        assert persisted.lease_expires_at is None
        assert persisted.retry_at is None
        assert not await store.publish(
            key="key",
            token=second.token or "expired-token",
            generation=second.generation or 0,
            response=_response(period),
            now=now + timedelta(seconds=4),
        )
        assert not await store.fail(
            key="key",
            token=second.token or "expired-token",
            generation=second.generation or 0,
            response=_response(period).model_copy(
                update={"status": AssessmentRunStatus.failed, "error": "late"}
            ),
            retry_after_seconds=1,
            now=now + timedelta(seconds=4),
        )
        repeated = await store.claim(
            key="key",
            ticker="TEST3",
            kind="stock",
            profile=None,
            period_at=period,
            now=now + timedelta(seconds=5),
        )
        assert repeated.state == "retry_exhausted"
        assert repeated.record.error == "retry_exhausted"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_schema_is_namespaced_and_venue_is_persisted(tmp_path: Path) -> None:
    path = tmp_path / "assessment.sqlite3"
    store = AssessmentStore(path)
    await store.startup()
    try:
        claim = await store.claim(
            key="venue-key",
            ticker="TEST3",
            kind="stock",
            profile=None,
            venue="BVMF",
            period_at=datetime(2026, 9, 12, 15, 0, tzinfo=UTC),
            now=datetime(2026, 9, 13, 15, 0, tzinfo=UTC),
        )
        assert claim.record.venue == "BVMF"
    finally:
        await store.close()

    with closing(sqlite3.connect(path)) as connection:
        names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert ASSESSMENT_TABLE in names
    assert "assessment_runs" not in names


@pytest.mark.asyncio
async def test_cleanup_removes_old_terminal_rows_but_keeps_live_processing(tmp_path: Path) -> None:
    path = tmp_path / "assessment.sqlite3"
    store = AssessmentStore(path)
    await store.startup()
    try:
        old = datetime(2025, 1, 1, 15, 0, tzinfo=UTC)
        processing = await store.claim(
            key="old-processing",
            ticker="TEST3",
            kind="stock",
            profile=None,
            period_at=old,
            now=datetime(2025, 1, 2, 15, 0, tzinfo=UTC),
        )
        terminal = await store.claim(
            key="old-terminal",
            ticker="TEST4",
            kind="stock",
            profile=None,
            period_at=old,
            now=datetime(2025, 1, 2, 15, 0, tzinfo=UTC),
        )
        assert processing.state == terminal.state == "claimed"
        assert terminal.token is not None and terminal.generation is not None
        assert await store.publish(
            key="old-terminal",
            token=terminal.token,
            generation=terminal.generation,
            response=_response(old, ticker="TEST4"),
            now=datetime(2025, 1, 2, 15, 0, tzinfo=UTC),
        )
        assert await store.cleanup_before(datetime(2026, 1, 1, tzinfo=UTC)) == 1
        assert await store.get("old-terminal") is None
        assert await store.get("old-processing") is not None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_startup_adds_identity_columns_to_an_existing_snapshot_database(
    tmp_path: Path,
) -> None:
    path = tmp_path / "assessment.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            f"""
            CREATE TABLE {ASSESSMENT_TABLE} (
                run_key TEXT PRIMARY KEY,
                ticker TEXT NOT NULL,
                kind TEXT NOT NULL,
                profile TEXT,
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
                updated_at TEXT NOT NULL
            )
            """
        )
    store = AssessmentStore(path)
    await store.startup()
    await store.close()
    with closing(sqlite3.connect(path)) as connection:
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({ASSESSMENT_TABLE})")}

    assert {
        "venue",
        "last_good_payload",
        "last_good_evidence_digest",
        "last_good_fetched_at",
    } <= columns


def test_assessment_record_round_trips_current_and_last_good_payloads() -> None:
    period = _period()
    payload = _response(period).model_dump(mode="json")
    record = AssessmentRecord(
        key="key",
        ticker="TEST3",
        kind="stock",
        profile=None,
        venue=None,
        period_at=period,
        status=AssessmentRunStatus.completed,
        attempts=1,
        generation=1,
        lease_token=None,
        lease_expires_at=None,
        retry_at=None,
        payload=payload,
        error=None,
        evidence_digest="digest",
        fetched_at=period,
        last_good_payload=payload,
        last_good_evidence_digest="digest",
        last_good_fetched_at=period,
    )

    assert record.response() == _response(period)
    assert record.last_good_response() == _response(period)
    assert (
        AssessmentRecord(
            key="empty",
            ticker="TEST3",
            kind="stock",
            profile=None,
            venue=None,
            period_at=period,
            status=AssessmentRunStatus.failed,
            attempts=1,
            generation=1,
            lease_token=None,
            lease_expires_at=None,
            retry_at=None,
            payload=None,
            error="failed",
            evidence_digest=None,
            fetched_at=None,
        ).response()
        is None
    )


@pytest.mark.asyncio
async def test_store_lifecycle_is_idempotent_and_requires_startup(tmp_path: Path) -> None:
    store = AssessmentStore(tmp_path / "assessment.sqlite3")
    with pytest.raises(RuntimeError, match="startup"):
        await store.get("missing")

    await store.startup()
    await store.startup()
    assert await store.get("missing") is None
    await store.close()
    await store.close()
    with pytest.raises(RuntimeError, match="startup"):
        await store.get("missing")


@pytest.mark.asyncio
async def test_store_serializes_concurrent_startup_and_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connections: list[aiosqlite.Connection] = []
    original_connect = aiosqlite.connect

    async def tracked_connect(database: str | Path) -> aiosqlite.Connection:
        connection = await original_connect(database)
        connections.append(connection)
        return connection

    monkeypatch.setattr(aiosqlite, "connect", tracked_connect)
    store = AssessmentStore(tmp_path / "assessment.sqlite3")

    await asyncio.gather(store.startup(), store.startup())

    assert len(connections) == 1
    assert store._db is connections[0]

    await asyncio.gather(store.close(), store.close())

    assert store._db is None
    assert store._started is False


@pytest.mark.asyncio
async def test_store_close_waits_for_active_sqlite_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = AssessmentStore(tmp_path / "assessment.sqlite3")
    await store.startup()
    db = store._db
    assert db is not None

    commit_started = asyncio.Event()
    release_commit = asyncio.Event()
    original_commit = db.commit

    async def blocked_commit() -> None:
        commit_started.set()
        await release_commit.wait()
        await original_commit()

    monkeypatch.setattr(db, "commit", blocked_commit)

    class LockProbe:
        def __init__(self, lock: asyncio.Lock) -> None:
            self._lock = lock
            self.close_waiting = asyncio.Event()
            self._acquire_count = 0

        async def __aenter__(self) -> LockProbe:
            self._acquire_count += 1
            if self._acquire_count == 2:
                self.close_waiting.set()
            await self._lock.acquire()
            return self

        async def __aexit__(self, *_args: object) -> None:
            self._lock.release()

    lock_probe = LockProbe(store._lock)
    monkeypatch.setattr(store, "_lock", lock_probe)

    claim_task = asyncio.create_task(
        store.claim(
            key="active-write",
            ticker="TEST3",
            kind="stock",
            profile=None,
            period_at=_period(),
        )
    )
    close_task: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(commit_started.wait(), timeout=1)
        close_task = asyncio.create_task(store.close())
        await asyncio.wait_for(lock_probe.close_waiting.wait(), timeout=1)
        assert not close_task.done()

        release_commit.set()
        result = await claim_task
        await close_task
        assert result.state == "claimed"
        assert store._db is None
        assert store._started is False
    finally:
        release_commit.set()
        if not claim_task.done():
            await asyncio.gather(claim_task, return_exceptions=True)
        if close_task is None:
            await store.close()
        elif not close_task.done():
            await asyncio.gather(close_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_store_failure_backoff_and_last_good_snapshot_are_durable(tmp_path: Path) -> None:
    now = datetime(2026, 9, 13, 15, 0, tzinfo=UTC)
    period = datetime(2026, 9, 12, 15, 0, tzinfo=UTC)
    store = AssessmentStore(tmp_path / "assessment.sqlite3", max_attempts=2)
    await store.startup()
    try:
        good = await store.claim(
            key="good",
            ticker="TEST3",
            kind="stock",
            profile=None,
            period_at=period,
            now=now,
        )
        assert good.token is not None and good.generation is not None
        good_response = _response(period).model_copy(
            update={
                "components": {
                    "quality": AssessmentComponentState(status=AssessmentComponentStatus.available)
                }
            }
        )
        assert await store.publish(
            key="good",
            token=good.token,
            generation=good.generation,
            response=good_response,
            now=now,
        )
        persisted = await store.get("good")
        assert persisted is not None
        assert persisted.last_good_response() == good_response

        first = await store.claim(
            key="retry",
            ticker="TEST3",
            kind="stock",
            profile=None,
            period_at=period,
            now=now,
        )
        assert first.token is not None and first.generation is not None
        failed_response = _response(period).model_copy(
            update={
                "status": AssessmentRunStatus.failed,
                "error": "Provider unavailable",
                "components": {
                    "assessment": AssessmentComponentState(
                        status=AssessmentComponentStatus.failed,
                        error_code="PROVIDER_UNAVAILABLE",
                        error="Provider unavailable",
                        retryable=True,
                    )
                },
            }
        )
        assert await store.fail(
            key="retry",
            token=first.token,
            generation=first.generation,
            response=failed_response,
            retry_after_seconds=1,
            now=now,
        )
        retry_wait = await store.claim(
            key="retry",
            ticker="TEST3",
            kind="stock",
            profile=None,
            period_at=period,
            now=now + timedelta(milliseconds=500),
        )
        assert retry_wait.state == "retry_wait"
        second = await store.claim(
            key="retry",
            ticker="TEST3",
            kind="stock",
            profile=None,
            period_at=period,
            now=now + timedelta(seconds=2),
        )
        assert second.state == "claimed"
        assert second.token is not None and second.generation is not None
        assert await store.fail(
            key="retry",
            token=second.token,
            generation=second.generation,
            response=failed_response,
            retry_after_seconds=1,
            now=now + timedelta(seconds=2),
        )
        exhausted = await store.claim(
            key="retry",
            ticker="TEST3",
            kind="stock",
            profile=None,
            period_at=period,
            now=now + timedelta(seconds=4),
        )
        assert exhausted.state == "retry_exhausted"
        assert (await store.get("retry")).response() == failed_response  # type: ignore[union-attr]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_store_cleanup_is_bounded_and_stale_failure_is_fenced(tmp_path: Path) -> None:
    now = datetime(2026, 9, 13, 15, 0, tzinfo=UTC)
    period = datetime(2025, 1, 1, 15, 0, tzinfo=UTC)
    store = AssessmentStore(tmp_path / "assessment.sqlite3")
    await store.startup()
    try:
        claims = [
            await store.claim(
                key=f"old-{index}",
                ticker="TEST3",
                kind="stock",
                profile=None,
                period_at=period,
                now=now,
            )
            for index in range(3)
        ]
        for index, claim in enumerate(claims):
            assert claim.token is not None and claim.generation is not None
            assert await store.publish(
                key=f"old-{index}",
                token=claim.token,
                generation=claim.generation,
                response=_response(period, ticker=f"TEST{index + 3}"),
                now=now,
            )
        assert await store.cleanup_before(now, batch_size=1) == 1
        assert await store.cleanup_before(now, batch_size=10) == 2

        live = await store.claim(
            key="live",
            ticker="TEST3",
            kind="stock",
            profile=None,
            period_at=period,
            now=now,
            lease_seconds=1,
        )
        assert live.token is not None and live.generation is not None
        assert not await store.fail(
            key="live",
            token=live.token,
            generation=live.generation,
            response=_response(period).model_copy(
                update={"status": AssessmentRunStatus.failed, "error": "late"}
            ),
            retry_after_seconds=1,
            now=now + timedelta(seconds=2),
        )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_maintenance_terminalizes_only_expired_closed_periods_before_cleanup(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 15, 18, 0, tzinfo=UTC)  # 15:00 in São Paulo
    closed_period = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)  # 12:00 slot
    open_period = datetime(2026, 9, 15, 17, 0, tzinfo=UTC)  # 14:00 slot, closed at 16:00
    store = AssessmentStore(tmp_path / "assessment.sqlite3")
    await store.startup()
    try:
        closed = await store.claim(
            key="closed",
            ticker="TEST3",
            kind="stock",
            profile=None,
            period_at=closed_period,
            now=now - timedelta(minutes=10),
            lease_seconds=1,
        )
        live = await store.claim(
            key="open",
            ticker="TEST4",
            kind="stock",
            profile=None,
            period_at=open_period,
            now=now - timedelta(minutes=10),
            lease_seconds=3600,
        )
        assert closed.state == live.state == "claimed"

        terminalized, removed = await store.maintenance(now, now=now, batch_size=10)

        assert terminalized == 1
        assert removed == 1
        closed_record = await store.get("closed")
        live_record = await store.get("open")
        assert closed_record is None
        assert live_record is not None
        assert live_record.status is AssessmentRunStatus.processing
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_maintenance_fences_expired_closed_claim_and_allows_retention_cleanup(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 15, 20, 0, tzinfo=UTC)  # 17:00 in São Paulo
    period = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)  # 12:00 slot is closed
    store = AssessmentStore(tmp_path / "assessment.sqlite3")
    await store.startup()
    try:
        claim = await store.claim(
            key="expired-closed",
            ticker="TEST3",
            kind="stock",
            profile=None,
            period_at=period,
            now=now - timedelta(minutes=10),
            lease_seconds=1,
        )
        assert claim.state == "claimed"
        terminalized, removed = await store.maintenance(now, now=now, batch_size=10)
        assert terminalized == 1
        assert removed == 1
        assert await store.get("expired-closed") is None
    finally:
        await store.close()


class _FakeAsyncContext:
    async def __aenter__(self) -> _FakeAsyncContext:
        return self

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        return None


class _FakePostgresCursor(_FakeAsyncContext):
    def __init__(self) -> None:
        self.rowcount = 1
        self.statements: list[str] = []
        self.pending_row: dict[str, object] | None = None

    async def execute(self, query: str, params: tuple[object, ...] = ()) -> None:
        self.statements.append(query)
        if "ON CONFLICT (run_key) DO NOTHING" in query and "RETURNING *" in query:
            values: tuple[object, ...] = tuple(params)
            columns = (
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
            self.pending_row = dict(zip(columns, values, strict=True))

    async def fetchone(self) -> dict[str, object] | None:
        row = self.pending_row
        self.pending_row = None
        return row


class _FakePostgresConnection(_FakeAsyncContext):
    def __init__(self) -> None:
        self.cursor_value = _FakePostgresCursor()
        self.statements: list[str] = []
        self.commits = 0

    async def execute(self, query: str, _params: object = ()) -> None:
        self.statements.append(query)

    def cursor(self, *, row_factory: object | None = None) -> _FakePostgresCursor:
        del row_factory
        return self.cursor_value

    def transaction(self) -> _FakeAsyncContext:
        return _FakeAsyncContext()

    async def commit(self) -> None:
        self.commits += 1


@pytest.mark.asyncio
async def test_postgres_store_paths_use_short_transactions(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.assessment import store as assessment_store

    connection = _FakePostgresConnection()

    async def connect(_database_url: str | None) -> _FakePostgresConnection:
        return connection

    monkeypatch.setattr(assessment_store, "_postgres_connect", connect)
    store = AssessmentStore(database_url="postgresql+asyncpg://user:pass@db/test")
    await store.startup()
    await store.startup()
    assert store.database_url == "postgresql://user:pass@db/test"
    assert await store.get("missing") is None

    now = datetime(2026, 9, 13, 15, 0, tzinfo=UTC)
    period = now - timedelta(days=1)
    claim = await store.claim(
        key="postgres",
        ticker="TEST3",
        kind="stock",
        profile=None,
        period_at=period,
        now=now,
    )
    assert claim.token is not None and claim.generation is not None
    assert await store.publish(
        key="postgres",
        token=claim.token,
        generation=claim.generation,
        response=_response(period),
        now=now,
    )
    second = await store.claim(
        key="postgres-failed",
        ticker="TEST3",
        kind="stock",
        profile=None,
        period_at=period,
        now=now,
    )
    assert second.token is not None and second.generation is not None
    assert await store.fail(
        key="postgres-failed",
        token=second.token,
        generation=second.generation,
        response=_response(period).model_copy(
            update={"status": AssessmentRunStatus.failed, "error": "provider"}
        ),
        retry_after_seconds=1,
        now=now,
    )
    assert await store.cleanup_before(now) == 1
    assert connection.commits == 1
    assert len(connection.statements) >= 5


def test_request_and_key_normalize_venue_without_colliding() -> None:
    period = _period()
    bvmf = AssessmentSnapshotRequest(
        ticker=" test3 ", kind=QualityAssetKind.stock, venue="bvmf", period_at=period
    )
    nasdaq = bvmf.model_copy(update={"venue": "NASDAQ"})
    renamed = bvmf.model_copy(update={"corporate_name": "A different public label"})
    profiled = bvmf.model_copy(update={"profile": "bank"})

    assert bvmf.ticker == "TEST3"
    assert bvmf.venue == "BVMF"
    assert assessment_key(bvmf) != assessment_key(nasdaq)
    assert assessment_key(bvmf) == assessment_key(renamed)
    assert assessment_key(bvmf) == assessment_key(profiled)


def test_request_rejects_reserved_unknown_venue_sentinel() -> None:
    with pytest.raises(ValidationError, match="venue '-' is reserved"):
        AssessmentSnapshotRequest(
            ticker="TEST3",
            kind=QualityAssetKind.stock,
            venue=" - ",
            period_at=_period(),
        )


@pytest.mark.parametrize(
    ("instrument", "expected"),
    [
        (None, None),
        (
            InstrumentMetadata(
                ticker="TEST3",
                name="Canonical Test Holdings",
                instrument_type=InstrumentType.stock,
                source="b3",
                confidence="high",
            ),
            "Canonical Test Holdings",
        ),
        (
            InstrumentMetadata(
                ticker="OTHER3",
                name="Wrong Ticker Holdings",
                instrument_type=InstrumentType.stock,
                source="b3",
                confidence="high",
            ),
            None,
        ),
        (
            InstrumentMetadata(
                ticker="TEST3",
                name="Directory Label",
                instrument_type=InstrumentType.stock,
                source="brapi_directory",
                confidence="medium",
            ),
            None,
        ),
        (
            InstrumentMetadata(
                ticker="TEST3",
                name="Low Confidence Label",
                instrument_type=InstrumentType.stock,
                source="b3",
                confidence="low",
            ),
            None,
        ),
        (
            InstrumentMetadata(
                ticker="TEST3",
                name="   ",
                instrument_type=InstrumentType.stock,
                source="b3",
                confidence="high",
            ),
            None,
        ),
    ],
)
def test_trusted_b3_name_rejects_unverified_or_mismatched_identity(
    instrument: InstrumentMetadata | None,
    expected: str | None,
) -> None:
    assert _trusted_b3_name("TEST3", instrument) == expected


def test_settings_reject_an_unbounded_assessment_lease() -> None:
    from pydantic import ValidationError

    from app.config import Settings

    assert Settings().assessment_lease_seconds == 3600
    with pytest.raises(ValidationError):
        Settings(assessment_lease_seconds=0)
    with pytest.raises(ValidationError):
        Settings(assessment_lease_seconds=3601)


def test_settings_treat_blank_optional_storage_paths_as_unset() -> None:
    from app.config import Settings

    settings = Settings(database_url="", assessment_sqlite_path="", income_store_url="")

    assert settings.database_url is None
    assert settings.income_store_url is None
    assert settings.assessment_sqlite_path is None
    assert settings.resolved_assessment_sqlite_path.name == "fundamentus_cache_assessments.sqlite3"
    custom = Settings(assessment_sqlite_path=Path("/tmp/custom-assessments.sqlite3"))
    assert custom.resolved_assessment_sqlite_path == Path("/tmp/custom-assessments.sqlite3")


def test_settings_read_the_income_store_url_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import Settings

    monkeypatch.setenv("INCOME_STORE_URL", "postgresql://user:pass@db/income")
    assert str(Settings().income_store_url) == "postgresql://user:pass@db/income"
    monkeypatch.delenv("INCOME_STORE_URL")
    monkeypatch.setenv("FUNDAMENTUS_API_INCOME_STORE_URL", "postgresql://user:pass@db/other")
    assert str(Settings().income_store_url) == "postgresql://user:pass@db/other"


@pytest.mark.asyncio
async def test_snapshot_service_resolves_opportunity_once_and_shares_it_with_quality(
    tmp_path: Path,
) -> None:
    class OpportunityStub:
        def __init__(self) -> None:
            self.calls = 0
            self.value = _opportunity("TEST3").model_copy(
                update={
                    "instrument": InstrumentMetadata(
                        ticker="TEST3",
                        name="Canonical Test Holdings S.A.",
                        instrument_type=InstrumentType.stock,
                        source="b3",
                        confidence="high",
                    )
                }
            )

        async def opportunity(self, _ticker: str) -> OpportunityResponse:
            self.calls += 1
            return self.value

    class FundamentalsStub:
        def __init__(self) -> None:
            self.calls = 0
            self.corporate_names: list[object] = []
            self.value = FundamentalsSnapshot(ticker="TEST3", periods=[])

        async def snapshot(
            self,
            ticker: str,
            corporate_name: object,
            **_kwargs: object,
        ) -> FundamentalsSnapshot:
            self.calls += 1
            self.corporate_names.append(corporate_name)
            return self.value.model_copy(update={"ticker": ticker})

    class QualityStub:
        def __init__(self) -> None:
            self.received: dict[str, OpportunityResponse | None] | None = None
            self.fundamentals: dict[str, FundamentalsSnapshot | None] | None = None
            self.request_profiles: list[str | None] = []

        async def resolve(
            self,
            request: QualityFactsRequest,
            *,
            opportunity_by_ticker: dict[str, OpportunityResponse | None] | None = None,
            fundamentals_by_ticker: dict[str, FundamentalsSnapshot | None] | None = None,
        ) -> QualityFactsResponse:
            self.request_profiles.append(request.assets[0].profile)
            self.received = opportunity_by_ticker
            self.fundamentals = fundamentals_by_ticker
            return QualityFactsResponse(
                assets=[
                    QualityAssetFacts(
                        ticker="TEST3",
                        kind=QualityAssetKind.stock,
                        profile="industrial",
                    )
                ],
                refreshed_at=datetime.now(UTC),
            )

    opportunity = OpportunityStub()
    fundamentals = FundamentalsStub()
    quality = QualityStub()
    service = AssessmentSnapshotService(
        AssessmentStore(tmp_path / "assessment.sqlite3"),
        opportunity,  # type: ignore[arg-type]
        fundamentals,  # type: ignore[arg-type]
        quality,  # type: ignore[arg-type]
    )
    await service.store.startup()
    try:
        request = AssessmentSnapshotRequest(
            ticker="TEST3",
            kind=QualityAssetKind.stock,
            profile="bank",
            corporate_name="  First Caller Holdings S.A. ",
            period_at=_period(),
        )
        result = await service.resolve(request)
        repeated = await service.resolve(
            request.model_copy(
                update={
                    "profile": "industrial",
                    "corporate_name": "Second Caller Holdings S.A.",
                }
            )
        )
        persisted = await service.store.get(assessment_key(request))
    finally:
        await service.store.close()

    assert result.status is AssessmentRunStatus.completed
    assert opportunity.calls == 1
    assert fundamentals.calls == 1
    assert fundamentals.corporate_names == ["Canonical Test Holdings S.A."]
    assert repeated == result
    assert persisted is not None
    assert persisted.profile is None
    assert result.profile == "industrial"
    assert quality.request_profiles == [None]
    assert quality.received == {"TEST3": opportunity.value}
    assert quality.fundamentals == {"TEST3": fundamentals.value}


@pytest.mark.asyncio
async def test_snapshot_service_keeps_fundamentals_independent_from_opportunity_failure(
    tmp_path: Path,
) -> None:
    class FailingOpportunity:
        calls = 0

        async def opportunity(self, _ticker: str) -> OpportunityResponse:
            self.calls += 1
            raise UpstreamUnavailableError()

    class FundamentalsStub:
        async def snapshot(
            self,
            ticker: str,
            *_args: object,
            **_kwargs: object,
        ) -> FundamentalsSnapshot:
            return FundamentalsSnapshot(ticker=ticker, periods=[])

    class QualityStub:
        async def resolve(
            self,
            _request: object,
            *,
            opportunity_by_ticker: dict[str, OpportunityResponse | None] | None = None,
            fundamentals_by_ticker: dict[str, FundamentalsSnapshot | None] | None = None,
        ) -> QualityFactsResponse:
            assert opportunity_by_ticker == {"TEST3": None}
            assert fundamentals_by_ticker == {
                "TEST3": FundamentalsSnapshot(ticker="TEST3", periods=[])
            }
            return QualityFactsResponse(
                assets=[QualityAssetFacts(ticker="TEST3", kind=QualityAssetKind.stock)],
                refreshed_at=datetime.now(UTC),
            )

    failing = FailingOpportunity()
    service = AssessmentSnapshotService(
        AssessmentStore(tmp_path / "assessment.sqlite3"),
        failing,  # type: ignore[arg-type]
        FundamentalsStub(),  # type: ignore[arg-type]
        QualityStub(),  # type: ignore[arg-type]
    )
    await service.store.startup()
    try:
        result = await service.resolve(
            AssessmentSnapshotRequest(
                ticker="TEST3",
                kind=QualityAssetKind.stock,
                period_at=_period(),
            )
        )
    finally:
        await service.store.close()

    assert result.status is AssessmentRunStatus.failed
    assert result.components["opportunity"].status.value == "failed"
    assert result.components["fundamentals"].status.value == "missing_data"


@pytest.mark.asyncio
async def test_snapshot_service_forwards_bdr_underlying_identity_to_fundamentals(
    tmp_path: Path,
) -> None:
    opportunity_value = _opportunity("AAPL34", InstrumentType.bdr).model_copy(
        update={
            "instrument": InstrumentMetadata(
                ticker="AAPL34",
                name="Apple BDR",
                instrument_type=InstrumentType.bdr,
                underlying_ticker="AAPL",
                underlying_name="Apple Inc.",
            )
        }
    )

    class FundamentalsStub:
        def __init__(self) -> None:
            self.kwargs: dict[str, object] = {}

        async def snapshot(
            self,
            ticker: str,
            corporate_name: str | None,
            **kwargs: object,
        ) -> FundamentalsSnapshot:
            assert ticker == "AAPL34"
            assert corporate_name == "Apple BDR"
            self.kwargs = kwargs
            return _available_fundamentals().model_copy(update={"ticker": ticker})

    fundamentals = FundamentalsStub()
    service = AssessmentSnapshotService(
        AssessmentStore(tmp_path / "assessment.sqlite3"),
        _AssessmentOpportunity(opportunity_value),  # type: ignore[arg-type]
        fundamentals,  # type: ignore[arg-type]
        _AssessmentQuality(QualityAssetFacts(ticker="AAPL34", kind=QualityAssetKind.stock)),  # type: ignore[arg-type]
    )
    await service.store.startup()
    try:
        result = await service.resolve(
            AssessmentSnapshotRequest(
                ticker="AAPL34",
                kind=QualityAssetKind.stock,
                venue="BVMF",
                period_at=_period(),
            )
        )
    finally:
        await service.store.close()

    assert result.status is AssessmentRunStatus.completed
    assert fundamentals.kwargs["instrument"] == opportunity_value.instrument
    assert fundamentals.kwargs["underlying_ticker"] == "AAPL"
    assert fundamentals.kwargs["underlying_name"] == "Apple Inc."


class _AssessmentOpportunity:
    def __init__(self, value: OpportunityResponse | None) -> None:
        self.value = value
        self.calls = 0

    async def opportunity(self, _ticker: str) -> OpportunityResponse | None:
        self.calls += 1
        if self.value is None:
            raise ValueError("provider response contained a secret")
        return self.value


class _AssessmentFundamentals:
    def __init__(self, snapshot: FundamentalsSnapshot) -> None:
        self.snapshot_value = snapshot

    async def snapshot(
        self,
        ticker: str,
        *_args: object,
        **_kwargs: object,
    ) -> FundamentalsSnapshot:
        return self.snapshot_value.model_copy(update={"ticker": ticker})


class _AssessmentQuality:
    def __init__(self, asset: QualityAssetFacts | None = None) -> None:
        self.asset = asset

    async def resolve(
        self,
        _request: object,
        *,
        opportunity_by_ticker: dict[str, OpportunityResponse | None] | None = None,
        fundamentals_by_ticker: dict[str, FundamentalsSnapshot | None] | None = None,
    ) -> QualityFactsResponse:
        del opportunity_by_ticker, fundamentals_by_ticker
        return QualityFactsResponse(
            assets=[] if self.asset is None else [self.asset],
            refreshed_at=datetime.now(UTC),
        )


def _available_fundamentals() -> FundamentalsSnapshot:
    period = FinancialPeriod(
        period_end=date(2025, 12, 31),
        consolidated=True,
        annual=True,
        revenue="100",
    )
    return FundamentalsSnapshot(ticker="TEST3", periods=[period])


@pytest.mark.asyncio
async def test_snapshot_service_handles_unsupported_etf_and_available_stock_paths(
    tmp_path: Path,
) -> None:
    instrument = InstrumentMetadata(ticker="TEST3", instrument_type=InstrumentType.stock)
    opportunity = _AssessmentOpportunity(_opportunity("TEST3", InstrumentType.stock))
    quality = _AssessmentQuality(QualityAssetFacts(ticker="TEST3", kind=QualityAssetKind.stock))
    service = AssessmentSnapshotService(
        AssessmentStore(tmp_path / "assessment.sqlite3"),
        opportunity,  # type: ignore[arg-type]
        _AssessmentFundamentals(_available_fundamentals()),  # type: ignore[arg-type]
        quality,  # type: ignore[arg-type]
    )
    await service.store.startup()
    try:
        crypto = await service.resolve(
            AssessmentSnapshotRequest(
                ticker="BTC",
                kind=QualityAssetKind.crypto,
                period_at=_period(),
            )
        )
        fixed = await service.resolve(
            AssessmentSnapshotRequest(
                ticker="TESOURO",
                kind=QualityAssetKind.fixed_income,
                period_at=_period(),
            )
        )
        etf = await service.resolve(
            AssessmentSnapshotRequest(
                ticker="ETF",
                kind=QualityAssetKind.etf,
                period_at=_period(),
            )
        )
        stock = await service.resolve(
            AssessmentSnapshotRequest(
                ticker="TEST3",
                kind=QualityAssetKind.stock,
                period_at=_period(),
            )
        )
    finally:
        await service.store.close()

    assert crypto.status is AssessmentRunStatus.completed
    assert all(
        state.status is AssessmentComponentStatus.unsupported
        for state in crypto.components.values()
    )
    assert fixed.status is AssessmentRunStatus.completed
    assert etf.components["opportunity"].status is AssessmentComponentStatus.unsupported
    assert etf.components["fundamentals"].status is AssessmentComponentStatus.unsupported
    assert etf.components["quality"].status is AssessmentComponentStatus.available
    assert stock.components["opportunity"].status is AssessmentComponentStatus.available
    assert stock.components["fundamentals"].status is AssessmentComponentStatus.available
    assert stock.components["quality"].status is AssessmentComponentStatus.available
    assert opportunity.calls == 1
    del instrument


@pytest.mark.asyncio
async def test_snapshot_service_reports_missing_components_and_sanitizes_errors(
    tmp_path: Path,
) -> None:
    opportunity = _AssessmentOpportunity(None)
    missing_fundamentals = FundamentalsSnapshot(ticker="TEST3", unavailable_reason="No filing")
    service = AssessmentSnapshotService(
        AssessmentStore(tmp_path / "assessment.sqlite3"),
        opportunity,  # type: ignore[arg-type]
        _AssessmentFundamentals(missing_fundamentals),  # type: ignore[arg-type]
        _AssessmentQuality(),  # type: ignore[arg-type]
    )
    await service.store.startup()
    try:
        result = await service.resolve(
            AssessmentSnapshotRequest(
                ticker="TEST3",
                kind=QualityAssetKind.stock,
                period_at=_period(),
            )
        )
    finally:
        await service.store.close()

    assert result.status is AssessmentRunStatus.failed
    assert result.components["opportunity"].error == "Component resolution failed."
    assert result.components["opportunity"].error_code == "VALUEERROR"


@pytest.mark.asyncio
async def test_snapshot_service_persists_a_confirmed_empty_public_snapshot(
    tmp_path: Path,
) -> None:
    empty_metrics = _opportunity("EMPTY3").metrics.model_copy(
        update={name: OpportunityMetric() for name in OpportunityMetrics.model_fields}
    )
    empty_opportunity = OpportunityResponse(
        ticker="EMPTY3",
        metrics=empty_metrics,
        refreshed_at=datetime.now(UTC),
    )
    store = AssessmentStore(tmp_path / "assessment.sqlite3")
    service = AssessmentSnapshotService(
        store,
        _AssessmentOpportunity(empty_opportunity),  # type: ignore[arg-type]
        _AssessmentFundamentals(
            FundamentalsSnapshot(ticker="EMPTY3", unavailable_reason="No filing")
        ),  # type: ignore[arg-type]
        _AssessmentQuality(
            QualityAssetFacts(
                ticker="EMPTY3",
                kind=QualityAssetKind.stock,
                unavailable_reason="No quality facts",
            )
        ),  # type: ignore[arg-type]
        retry_backoff_seconds=30,
    )
    await store.startup()
    request = AssessmentSnapshotRequest(
        ticker="EMPTY3",
        kind=QualityAssetKind.stock,
        period_at=_period(),
    )
    try:
        result = await service.resolve(request)
        persisted = await store.get(assessment_key(request))
        repeated = await service.resolve(request)
    finally:
        await store.close()

    assert result.status is AssessmentRunStatus.completed
    assert persisted is not None
    assert persisted.status is AssessmentRunStatus.completed
    assert persisted.attempts == 1
    assert repeated == result


@pytest.mark.asyncio
async def test_snapshot_service_retries_empty_opportunity_after_source_outage(
    tmp_path: Path,
) -> None:
    empty_metrics = _opportunity("OPP3").metrics.model_copy(
        update={name: OpportunityMetric() for name in OpportunityMetrics.model_fields}
    )
    unavailable = OpportunityResponse(
        ticker="OPP3",
        metrics=empty_metrics,
        source_failures={"b3": "PROVIDER_UNAVAILABLE", "status_invest": "PROVIDER_UNAVAILABLE"},
        refreshed_at=datetime.now(UTC),
    )

    class ToggleOpportunity:
        def __init__(self) -> None:
            self.failed = True
            self.calls = 0

        async def opportunity(self, _ticker: str) -> OpportunityResponse:
            self.calls += 1
            return unavailable if self.failed else _opportunity("OPP3")

    opportunity = ToggleOpportunity()
    store = AssessmentStore(tmp_path / "assessment.sqlite3")
    service = AssessmentSnapshotService(
        store,
        opportunity,  # type: ignore[arg-type]
        _AssessmentFundamentals(_available_fundamentals()),  # type: ignore[arg-type]
        _AssessmentQuality(QualityAssetFacts(ticker="OPP3", kind=QualityAssetKind.stock)),  # type: ignore[arg-type]
        retry_backoff_seconds=0,
    )
    request = AssessmentSnapshotRequest(
        ticker="OPP3",
        kind=QualityAssetKind.stock,
        period_at=_period(),
    )
    await store.startup()
    try:
        first = await service.resolve(request)
        failed_record = await store.get(assessment_key(request))
        opportunity.failed = False
        second = await service.resolve(request)
    finally:
        await store.close()

    assert first.status is AssessmentRunStatus.failed
    failed_state = first.components["opportunity"]
    assert failed_state.status is AssessmentComponentStatus.failed
    assert failed_state.error_code == "OPPORTUNITY_SOURCES_UNAVAILABLE"
    assert failed_state.retryable is True
    assert failed_record is not None
    assert failed_record.status is AssessmentRunStatus.failed
    assert second.status is AssessmentRunStatus.completed
    assert opportunity.calls == 2


@pytest.mark.asyncio
async def test_snapshot_service_persists_unexpected_build_failure_and_handles_lost_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = AssessmentSnapshotService(
        AssessmentStore(tmp_path / "assessment.sqlite3"),
        _AssessmentOpportunity(_opportunity("TEST3")),  # type: ignore[arg-type]
        _AssessmentFundamentals(_available_fundamentals()),  # type: ignore[arg-type]
        _AssessmentQuality(QualityAssetFacts(ticker="TEST3", kind=QualityAssetKind.stock)),  # type: ignore[arg-type]
        retry_backoff_seconds=0,
    )
    await service.store.startup()
    try:

        async def broken_build(_request: AssessmentSnapshotRequest) -> AssessmentSnapshotResponse:
            raise RuntimeError("secret provider response")

        monkeypatch.setattr(service, "_build", broken_build)
        failed = await service.resolve(
            AssessmentSnapshotRequest(
                ticker="TEST3",
                kind=QualityAssetKind.stock,
                period_at=_period(),
            )
        )
        assert failed.status is AssessmentRunStatus.failed
        assert failed.error == "Component resolution failed."

        async def completed_build(request: AssessmentSnapshotRequest) -> AssessmentSnapshotResponse:
            return _response(request.period_at)

        monkeypatch.setattr(service, "_build", completed_build)

        async def fenced_publish(**_kwargs: object) -> bool:
            return False

        monkeypatch.setattr(service.store, "publish", fenced_publish)
        processing = await service.resolve(
            AssessmentSnapshotRequest(
                ticker="TEST4",
                kind=QualityAssetKind.stock,
                period_at=_period(),
            )
        )
    finally:
        await service.store.close()

    assert processing.status is AssessmentRunStatus.processing


def test_assessment_private_state_helpers_cover_error_and_status_rules() -> None:
    request = AssessmentSnapshotRequest(
        ticker="TEST3",
        kind=QualityAssetKind.stock,
        period_at=_period(),
    )
    api_state = _error_state(UpstreamUnavailableError())
    generic_state = _error_state(ValueError("hidden"))
    assert api_state.error_code == "UPSTREAM_UNAVAILABLE"
    assert api_state.retryable is True
    assert generic_state.error == "Component resolution failed."
    assert (
        _overall_status(iter([AssessmentComponentState(status=AssessmentComponentStatus.failed)]))
        is AssessmentRunStatus.failed
    )
    assert (
        _overall_status(
            iter([AssessmentComponentState(status=AssessmentComponentStatus.missing_data)])
        )
        is AssessmentRunStatus.completed
    )
    assert (
        _overall_status(
            iter([AssessmentComponentState(status=AssessmentComponentStatus.unsupported)])
        )
        is AssessmentRunStatus.completed
    )
    assert assessment_key(request).startswith("assessment:v5:TEST3:stock")

    service = AssessmentSnapshotService(
        AssessmentStore(Path("/tmp/unused-assessment.sqlite3")),
        _AssessmentOpportunity(None),  # type: ignore[arg-type]
        _AssessmentFundamentals(_available_fundamentals()),  # type: ignore[arg-type]
        _AssessmentQuality(),  # type: ignore[arg-type]
        period_history_days=0,
        retry_backoff_seconds=-1,
    )
    assert service.period_history_days == 1
    assert service.retry_backoff_seconds == 0
    assert _remaining_seconds(None) is None
    assert _remaining_seconds(datetime.now(UTC) - timedelta(seconds=1)) == 0
    with pytest.raises(InvalidAssessmentPeriodError, match="future"):
        service._validate_period(
            datetime.now(ASSESSMENT_TIMEZONE).replace(hour=19, minute=0, second=0, microsecond=0)
            + timedelta(days=1)
        )

    with pytest.raises(InvalidAssessmentPeriodError, match="retention"):
        service._validate_period(
            datetime.now(ASSESSMENT_TIMEZONE).replace(hour=12, minute=0, second=0, microsecond=0)
            - timedelta(days=2)
        )

    malformed = AssessmentSnapshotRequest.model_construct(
        ticker="bad ticker",
        kind=QualityAssetKind.stock,
        period_at=_period(),
    )
    with pytest.raises(InvalidTickerError):
        service._normalize_request(malformed)
    normalized = AssessmentSnapshotRequest.model_construct(
        ticker="test3",
        kind=QualityAssetKind.stock,
        profile="bank",
        period_at=_period(),
    )
    normalized_request = service._normalize_request(normalized)
    assert normalized_request.ticker == "TEST3"
    assert normalized_request.profile is None


def test_assessment_window_state_matches_sao_paulo_slot_boundaries() -> None:
    period = datetime(2026, 9, 15, 12, 0, tzinfo=ASSESSMENT_TIMEZONE)
    assert (
        _assessment_window_state(period, datetime(2026, 9, 15, 11, 59, tzinfo=ASSESSMENT_TIMEZONE))
        == "future"
    )
    assert (
        _assessment_window_state(period, datetime(2026, 9, 15, 12, 0, tzinfo=ASSESSMENT_TIMEZONE))
        == "open"
    )
    assert (
        _assessment_window_state(
            period, datetime(2026, 9, 15, 13, 59, 59, tzinfo=ASSESSMENT_TIMEZONE)
        )
        == "open"
    )
    assert (
        _assessment_window_state(period, datetime(2026, 9, 15, 14, 0, tzinfo=ASSESSMENT_TIMEZONE))
        == "closed"
    )

    evening_period = datetime(2026, 9, 15, 19, 0, tzinfo=ASSESSMENT_TIMEZONE)
    assert (
        _assessment_window_state(
            evening_period, datetime(2026, 9, 15, 23, 59, 59, tzinfo=ASSESSMENT_TIMEZONE)
        )
        == "open"
    )
    assert (
        _assessment_window_state(
            evening_period, datetime(2026, 9, 16, 0, 0, tzinfo=ASSESSMENT_TIMEZONE)
        )
        == "closed"
    )


@pytest.mark.asyncio
async def test_snapshot_service_rejects_missing_closed_slot_before_claim_or_provider_io(
    tmp_path: Path,
) -> None:
    class UncalledOpportunity:
        calls = 0

        async def opportunity(self, _ticker: str) -> OpportunityResponse:
            self.calls += 1
            raise AssertionError("closed slots must not call providers")

    class UncalledFundamentals:
        calls = 0

        async def snapshot(
            self,
            _ticker: str,
            *_args: object,
            **_kwargs: object,
        ) -> FundamentalsSnapshot:
            self.calls += 1
            raise AssertionError("closed slots must not call providers")

    class UncalledQuality:
        calls = 0

        async def resolve(
            self,
            _request: object,
            **_kwargs: object,
        ) -> QualityFactsResponse:
            self.calls += 1
            raise AssertionError("closed slots must not call providers")

    opportunity = UncalledOpportunity()
    fundamentals = UncalledFundamentals()
    quality = UncalledQuality()
    store = AssessmentStore(tmp_path / "assessment.sqlite3")
    service = AssessmentSnapshotService(
        store,
        opportunity,  # type: ignore[arg-type]
        fundamentals,  # type: ignore[arg-type]
        quality,  # type: ignore[arg-type]
    )
    closed_period = datetime(2026, 9, 14, 12, 0, tzinfo=ASSESSMENT_TIMEZONE)
    request = AssessmentSnapshotRequest(
        ticker="CLOSED3",
        kind=QualityAssetKind.stock,
        period_at=closed_period,
    )
    await store.startup()
    try:
        with pytest.raises(InvalidAssessmentPeriodError, match="supported scheduler window"):
            await service.resolve(request)
        assert await store.get(assessment_key(request)) is None
    finally:
        await store.close()

    assert opportunity.calls == 0
    assert fundamentals.calls == 0
    assert quality.calls == 0


@pytest.mark.asyncio
async def test_snapshot_service_reads_existing_closed_slot_within_retention(
    tmp_path: Path,
) -> None:
    closed_period = datetime(2026, 9, 14, 12, 0, tzinfo=ASSESSMENT_TIMEZONE)
    request = AssessmentSnapshotRequest(
        ticker="STORED3",
        kind=QualityAssetKind.stock,
        period_at=closed_period,
    )
    store = AssessmentStore(tmp_path / "assessment.sqlite3")
    await store.startup()
    claim = await store.claim(
        key=assessment_key(request),
        ticker=request.ticker,
        kind=request.kind.value,
        profile=None,
        venue=request.venue,
        period_at=request.period_at,
    )
    assert claim.token is not None and claim.generation is not None
    stored = _response(request.period_at, ticker=request.ticker)
    assert await store.publish(
        key=assessment_key(request),
        token=claim.token,
        generation=claim.generation,
        response=stored,
    )

    class UncalledOpportunity:
        async def opportunity(self, _ticker: str) -> OpportunityResponse:
            raise AssertionError("stored snapshots must not call providers")

    class UncalledFundamentals:
        async def snapshot(
            self,
            _ticker: str,
            *_args: object,
            **_kwargs: object,
        ) -> FundamentalsSnapshot:
            raise AssertionError("stored snapshots must not call providers")

    class UncalledQuality:
        async def resolve(self, _request: object, **_kwargs: object) -> QualityFactsResponse:
            raise AssertionError("stored snapshots must not call providers")

    service = AssessmentSnapshotService(
        store,
        UncalledOpportunity(),  # type: ignore[arg-type]
        UncalledFundamentals(),  # type: ignore[arg-type]
        UncalledQuality(),  # type: ignore[arg-type]
    )
    try:
        result = await service.resolve(request)
    finally:
        await store.close()

    assert result == stored


def test_assessment_request_rejects_naive_and_non_scheduler_periods() -> None:
    with pytest.raises(ValidationError, match="timezone offset"):
        AssessmentSnapshotRequest(
            ticker="TEST3",
            kind=QualityAssetKind.stock,
            period_at=datetime(2026, 9, 12, 12, 0),
        )
    with pytest.raises(ValidationError, match="one of 12:00"):
        AssessmentSnapshotRequest(
            ticker="TEST3",
            kind=QualityAssetKind.stock,
            period_at=datetime(2026, 9, 12, 13, 0, tzinfo=ASSESSMENT_TIMEZONE),
        )
    with pytest.raises(ValidationError, match="control characters"):
        AssessmentSnapshotRequest(
            ticker="TEST3",
            kind=QualityAssetKind.stock,
            corporate_name="Issuer\x00Name",
            period_at=_period(),
        )

    period = _period()
    payload = response_payload(_response(period))
    assert payload["ticker"] == "TEST3"
    assert payload["period_at"] == period.isoformat().replace("+00:00", "Z")


def test_assessment_quality_models_reject_nonfinite_and_out_of_range_values() -> None:
    with pytest.raises(ValidationError):
        QualityFactObservation(as_of=date(2026, 9, 12), value=Decimal("NaN"))
    with pytest.raises(ValueError, match="observation must be finite"):
        QualityFactObservation.finite_value(Decimal("NaN"))
    with pytest.raises(ValueError, match="values must be finite"):
        QualityFact.finite_numbers(Decimal("NaN"))
    with pytest.raises(ValueError, match="between zero and one"):
        QualityFact.confidence_range(Decimal("1.1"))
    with pytest.raises(ValueError, match="non-negative"):
        QualityFact.non_negative_source_count(-1)


def test_assessment_dependency_returns_application_service_from_request() -> None:
    service = object()
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(assessment_service=service))
    )

    assert get_assessment_service(request) is service  # type: ignore[arg-type]


def test_assessment_record_without_last_good_payload_returns_none() -> None:
    record = AssessmentRecord(
        key="empty",
        ticker="TEST3",
        kind="stock",
        profile=None,
        venue=None,
        period_at=_period(),
        status=AssessmentRunStatus.failed,
        attempts=1,
        generation=1,
        lease_token=None,
        lease_expires_at=None,
        retry_at=None,
        payload=None,
        error="provider unavailable",
        evidence_digest=None,
        fetched_at=None,
    )

    assert record.last_good_response() is None


def test_assessment_store_helpers_normalize_database_urls_and_publishability() -> None:
    assert _normalize_database_url("postgres+asyncpg://db/app") == "postgresql://db/app"
    assert _normalize_database_url("postgresql+asyncpg://db/app") == "postgresql://db/app"
    assert _normalize_database_url("postgres+psycopg://db/app") == "postgresql://db/app"
    assert _normalize_database_url("postgresql+psycopg://db/app") == "postgresql://db/app"
    assert _normalize_database_url(" sqlite://local ") == "sqlite://local"
    assert _normalize_database_url("") is None
    cursor = type("_Cursor", (), {"pgresult": None})()
    assert callable(_postgres_row_factory(cursor))
    assert not _is_publishable(
        _response(_period()).model_copy(update={"status": AssessmentRunStatus.failed})
    )


@pytest.mark.asyncio
async def test_postgres_connect_requires_a_configured_database_url() -> None:
    with pytest.raises(RuntimeError, match="database URL is required"):
        await _postgres_connect(None)


@pytest.mark.asyncio
async def test_snapshot_service_reuses_completed_rows_and_reports_live_claims_and_retries(
    tmp_path: Path,
) -> None:
    store = AssessmentStore(tmp_path / "assessment.sqlite3")
    service = AssessmentSnapshotService(
        store,
        _AssessmentOpportunity(_opportunity("TEST3")),  # type: ignore[arg-type]
        _AssessmentFundamentals(_available_fundamentals()),  # type: ignore[arg-type]
        _AssessmentQuality(QualityAssetFacts(ticker="TEST3", kind=QualityAssetKind.stock)),  # type: ignore[arg-type]
        retry_backoff_seconds=30,
    )
    await store.startup()
    period = _period()
    try:
        in_progress_request = AssessmentSnapshotRequest(
            ticker="TEST3",
            kind=QualityAssetKind.stock,
            profile="live",
            period_at=period,
        )
        preclaim = await store.claim(
            key=assessment_key(in_progress_request),
            ticker=in_progress_request.ticker,
            kind=in_progress_request.kind.value,
            profile=None,
            period_at=period,
        )
        assert preclaim.state == "claimed"
        live = await service.resolve(in_progress_request)
        assert live.status is AssessmentRunStatus.processing
        assert "already in progress" in (live.error or "")

        completed_request = in_progress_request.model_copy(
            update={"profile": "completed", "venue": "NASDAQ"}
        )
        first = await service.resolve(completed_request)
        second = await service.resolve(completed_request)
        assert first.status is AssessmentRunStatus.completed
        assert second.status is AssessmentRunStatus.completed
        assert second.evidence_digest == first.evidence_digest

        retry_request = in_progress_request.model_copy(update={"profile": "retry", "venue": "NYSE"})
        retry_claim = await store.claim(
            key=assessment_key(retry_request),
            ticker=retry_request.ticker,
            kind=retry_request.kind.value,
            profile=None,
            period_at=period,
        )
        assert retry_claim.token is not None and retry_claim.generation is not None
        failed = _response(period).model_copy(
            update={
                "status": AssessmentRunStatus.failed,
                "error": "temporary provider outage",
            }
        )
        assert await store.fail(
            key=assessment_key(retry_request),
            token=retry_claim.token,
            generation=retry_claim.generation,
            response=failed,
            retry_after_seconds=60,
        )
        waiting = await service.resolve(retry_request)
        assert waiting.status is AssessmentRunStatus.failed
        assert waiting.retry_after_seconds is not None
        assert waiting.retry_after_seconds > 0
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_snapshot_service_keeps_component_failures_and_missing_quality_explicit(
    tmp_path: Path,
) -> None:
    class FailingFundamentals:
        async def snapshot(
            self,
            _ticker: str,
            *_args: object,
            **_kwargs: object,
        ) -> FundamentalsSnapshot:
            raise UpstreamUnavailableError()

    class FailingQuality:
        async def resolve(
            self,
            _request: object,
            *,
            opportunity_by_ticker: dict[str, OpportunityResponse | None] | None = None,
            fundamentals_by_ticker: dict[str, FundamentalsSnapshot | None] | None = None,
        ) -> QualityFactsResponse:
            del opportunity_by_ticker, fundamentals_by_ticker
            raise UpstreamUnavailableError()

    async def run(
        fundamentals: object,
        quality: object,
        ticker: str,
    ) -> AssessmentSnapshotResponse:
        store = AssessmentStore(tmp_path / f"{ticker}.sqlite3")
        service = AssessmentSnapshotService(
            store,
            _AssessmentOpportunity(_opportunity(ticker)),  # type: ignore[arg-type]
            fundamentals,  # type: ignore[arg-type]
            quality,  # type: ignore[arg-type]
        )
        await store.startup()
        try:
            return await service.resolve(
                AssessmentSnapshotRequest(
                    ticker=ticker,
                    kind=QualityAssetKind.stock,
                    period_at=_period(),
                )
            )
        finally:
            await store.close()

    fundamentals_failed = await run(
        FailingFundamentals(),
        _AssessmentQuality(QualityAssetFacts(ticker="FUND3", kind=QualityAssetKind.stock)),
        "FUND3",
    )
    assert fundamentals_failed.status is AssessmentRunStatus.failed
    assert fundamentals_failed.components["fundamentals"].status is AssessmentComponentStatus.failed

    quality_failed = await run(
        _AssessmentFundamentals(_available_fundamentals()),
        FailingQuality(),
        "QUAL3",
    )
    assert quality_failed.status is AssessmentRunStatus.failed
    assert quality_failed.components["quality"].status is AssessmentComponentStatus.failed

    quality_missing = await run(
        _AssessmentFundamentals(_available_fundamentals()),
        _AssessmentQuality(
            QualityAssetFacts(
                ticker="MISS3",
                kind=QualityAssetKind.stock,
                unavailable_reason="Quality source did not publish this period",
            )
        ),
        "MISS3",
    )
    assert quality_missing.status is AssessmentRunStatus.completed
    assert quality_missing.components["quality"].status is AssessmentComponentStatus.missing_data

    quality_provider_failure = await run(
        _AssessmentFundamentals(_available_fundamentals()),
        _AssessmentQuality(
            QualityAssetFacts(
                ticker="FAIL3",
                kind=QualityAssetKind.stock,
                unavailable_reason="Quality evidence unavailable",
                error_code="QUALITY_RESOLUTION_FAILED",
                retryable=True,
            )
        ),
        "FAIL3",
    )
    assert quality_provider_failure.status is AssessmentRunStatus.failed
    quality_state = quality_provider_failure.components["quality"]
    assert quality_state.status is AssessmentComponentStatus.failed
    assert quality_state.error_code == "QUALITY_RESOLUTION_FAILED"
    assert quality_state.retryable is True


def test_quality_missing_data_does_not_override_failed_overall_status() -> None:
    quality_failure = AssessmentComponentState(
        status=AssessmentComponentStatus.failed,
        error_code="QUALITY_RESOLUTION_FAILED",
        retryable=True,
    )
    assert (
        _overall_status(
            (
                AssessmentComponentState(status=AssessmentComponentStatus.missing_data),
                quality_failure,
            )
        )
        is AssessmentRunStatus.failed
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_component", ["fundamentals", "quality"])
async def test_snapshot_service_retries_failed_component_before_publishing_completed_snapshot(
    tmp_path: Path,
    failed_component: str,
) -> None:
    class ToggleFundamentals:
        def __init__(self) -> None:
            self.failed = failed_component == "fundamentals"
            self.calls = 0

        async def snapshot(
            self,
            ticker: str,
            *_args: object,
            **_kwargs: object,
        ) -> FundamentalsSnapshot:
            self.calls += 1
            if self.failed:
                raise UpstreamUnavailableError()
            return _available_fundamentals().model_copy(update={"ticker": ticker})

    class ToggleQuality:
        def __init__(self) -> None:
            self.failed = failed_component == "quality"
            self.calls = 0

        async def resolve(
            self,
            _request: object,
            *,
            opportunity_by_ticker: dict[str, OpportunityResponse | None] | None = None,
            fundamentals_by_ticker: dict[str, FundamentalsSnapshot | None] | None = None,
        ) -> QualityFactsResponse:
            del opportunity_by_ticker, fundamentals_by_ticker
            self.calls += 1
            if self.failed:
                raise UpstreamUnavailableError()
            return QualityFactsResponse(
                assets=[QualityAssetFacts(ticker="RECOVER3", kind=QualityAssetKind.stock)],
                refreshed_at=datetime.now(UTC),
            )

    fundamentals = ToggleFundamentals()
    quality = ToggleQuality()
    store = AssessmentStore(tmp_path / "assessment.sqlite3")
    service = AssessmentSnapshotService(
        store,
        _AssessmentOpportunity(_opportunity("RECOVER3")),  # type: ignore[arg-type]
        fundamentals,  # type: ignore[arg-type]
        quality,  # type: ignore[arg-type]
        retry_backoff_seconds=0,
    )
    request = AssessmentSnapshotRequest(
        ticker="RECOVER3",
        kind=QualityAssetKind.stock,
        period_at=_period(),
    )
    await store.startup()
    try:
        first = await service.resolve(request)
        failed_record = await store.get(assessment_key(request))
        assert first.status is AssessmentRunStatus.failed
        assert failed_record is not None
        assert failed_record.status is AssessmentRunStatus.failed

        fundamentals.failed = False
        quality.failed = False
        second = await service.resolve(request)
        completed_record = await store.get(assessment_key(request))
    finally:
        await store.close()

    assert second.status is AssessmentRunStatus.completed
    assert completed_record is not None
    assert completed_record.status is AssessmentRunStatus.completed
    assert completed_record.attempts == 2
    assert fundamentals.calls == 2
    assert quality.calls == 2


@pytest.mark.asyncio
async def test_fundamentals_route_falls_back_when_opportunity_enrichment_is_unavailable() -> None:
    class UnavailableOpportunity:
        async def opportunity(self, ticker: str) -> OpportunityResponse:
            raise ProviderUnavailableError(ticker=ticker)

    class Fundamentals:
        async def snapshot(
            self,
            ticker: str,
            *args: object,
            **kwargs: object,
        ) -> FundamentalsSnapshot:
            assert args == (None,)
            assert kwargs["reference_shares"] is None
            return FundamentalsSnapshot(ticker=ticker, periods=[])

    result = await _resolve_fundamentals(
        "TEST3",
        Fundamentals(),  # type: ignore[arg-type]
        UnavailableOpportunity(),  # type: ignore[arg-type]
    )

    assert result.ticker == "TEST3"
    assert result.snapshot is not None
    assert result.snapshot.ticker == "TEST3"


@pytest.mark.asyncio
async def test_fundamentals_route_rethrows_invalid_tickers() -> None:
    class InvalidOpportunity:
        async def opportunity(self, ticker: str) -> OpportunityResponse:
            raise InvalidTickerError(ticker=ticker)

    class Fundamentals:
        async def snapshot(self, *_args: object, **_kwargs: object) -> FundamentalsSnapshot:
            raise AssertionError("invalid tickers must not reach fundamentals")

    with pytest.raises(InvalidTickerError):
        await _resolve_fundamentals(
            "BAD3",
            Fundamentals(),  # type: ignore[arg-type]
            InvalidOpportunity(),  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_fundamentals_batch_route_returns_typed_fallbacks_for_each_failure() -> None:
    class BatchOpportunity:
        async def opportunity(self, ticker: str) -> OpportunityResponse | None:
            return None

    class BatchFundamentals:
        async def snapshot(
            self,
            ticker: str,
            *_args: object,
            **_kwargs: object,
        ) -> FundamentalsSnapshot:
            if ticker == "API3":
                raise ProviderUnavailableError(ticker=ticker)
            if ticker == "ERR3":
                raise ValueError("malformed filing")
            return FundamentalsSnapshot(ticker=ticker, periods=[])

    response = Response()
    result = await resolve_fundamentals_batch(
        FundamentalsBatchRequest(tickers=["GOOD3", "API3", "ERR3"]),
        BatchFundamentals(),  # type: ignore[arg-type]
        BatchOpportunity(),  # type: ignore[arg-type]
        response,
    )

    assert [asset.ticker for asset in result.assets] == ["GOOD3", "API3", "ERR3"]
    assert result.assets[1].snapshot is not None
    assert result.assets[1].snapshot.unavailable_reason == "A data provider is unavailable."
    assert result.assets[2].snapshot is not None
    assert result.assets[2].snapshot.unavailable_reason == "Fundamentals resolution failed"
    assert response.headers["Cache-Control"].startswith("private")


@pytest.mark.asyncio
async def test_fundamentals_batch_route_rethrows_invalid_tickers() -> None:
    class InvalidOpportunity:
        async def opportunity(self, ticker: str) -> OpportunityResponse | None:
            if ticker == "BAD3":
                raise InvalidTickerError(ticker=ticker)
            return None

    class Fundamentals:
        async def snapshot(
            self,
            ticker: str,
            *_args: object,
            **_kwargs: object,
        ) -> FundamentalsSnapshot:
            return FundamentalsSnapshot(ticker=ticker, periods=[])

    with pytest.raises(InvalidTickerError):
        await resolve_fundamentals_batch(
            FundamentalsBatchRequest(tickers=["GOOD3", "BAD3"]),
            Fundamentals(),  # type: ignore[arg-type]
            InvalidOpportunity(),  # type: ignore[arg-type]
            Response(),
        )


@pytest.mark.asyncio
async def test_fixed_income_names_are_normalized_and_shared_without_market_resolvers(
    tmp_path: Path,
) -> None:
    class UncalledOpportunity:
        def __init__(self) -> None:
            self.calls = 0

        async def opportunity(self, _ticker: str) -> OpportunityResponse:
            self.calls += 1
            raise AssertionError("fixed-income assessments must not call market opportunity")

    class UncalledFundamentals:
        def __init__(self) -> None:
            self.calls = 0

        async def snapshot(
            self,
            _ticker: str,
            *_args: object,
            **_kwargs: object,
        ) -> FundamentalsSnapshot:
            self.calls += 1
            raise AssertionError("fixed-income assessments must not call fundamentals")

    class UncalledQuality:
        def __init__(self) -> None:
            self.calls = 0

        async def resolve(
            self,
            _request: object,
            *,
            opportunity_by_ticker: dict[str, OpportunityResponse | None] | None = None,
            fundamentals_by_ticker: dict[str, FundamentalsSnapshot | None] | None = None,
        ) -> QualityFactsResponse:
            del opportunity_by_ticker, fundamentals_by_ticker
            self.calls += 1
            raise AssertionError("fixed-income assessments must not call quality")

    opportunity = UncalledOpportunity()
    fundamentals = UncalledFundamentals()
    quality = UncalledQuality()
    store = AssessmentStore(tmp_path / "assessment.sqlite3")
    service = AssessmentSnapshotService(
        store,
        opportunity,  # type: ignore[arg-type]
        fundamentals,  # type: ignore[arg-type]
        quality,  # type: ignore[arg-type]
    )
    await store.startup()
    try:
        request = AssessmentSnapshotRequest(
            ticker="  Tesouro   IPCA+ 2029 ",
            kind=QualityAssetKind.fixed_income,
            period_at=_period(),
        )
        assert request.ticker == "TESOURO IPCA+ 2029"
        first = await service.resolve(request)
        second = await service.resolve(
            AssessmentSnapshotRequest(
                ticker="TESOURO IPCA+ 2029",
                kind=QualityAssetKind.fixed_income,
                period_at=request.period_at,
            )
        )
        persisted = await store.get(assessment_key(request))
    finally:
        await store.close()

    assert first.status is AssessmentRunStatus.completed
    assert first.ticker == "TESOURO IPCA+ 2029"
    assert second.status is AssessmentRunStatus.completed
    assert second.ticker == "TESOURO IPCA+ 2029"
    assert persisted is not None
    assert persisted.response() == second
    assert opportunity.calls == fundamentals.calls == quality.calls == 0
