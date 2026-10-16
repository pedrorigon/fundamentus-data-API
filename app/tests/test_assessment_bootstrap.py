from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.assessment import store as assessment_store
from app.assessment.models import (
    ASSESSMENT_TIMEZONE,
    AssessmentComponentState,
    AssessmentComponentStatus,
    AssessmentMode,
    AssessmentRunStatus,
    AssessmentSnapshotRequest,
    AssessmentSnapshotResponse,
)
from app.assessment.service import (
    AssessmentSnapshotService,
    InvalidAssessmentPeriodError,
    assessment_key,
    bootstrap_assessment_key,
)
from app.assessment.store import (
    AssessmentRecord,
    AssessmentStore,
    BootstrapWork,
    BootstrapWorkClaim,
    ClaimResult,
    _prepare_bootstrap_work,
)
from app.assessment.worker import BootstrapAssessmentWorker
from app.models.quality import QualityAssetKind


def _request(
    *,
    ticker: str = "TEST3",
    bootstrap_key: str = "a" * 64,
    period_at: datetime | None = None,
) -> AssessmentSnapshotRequest:
    return AssessmentSnapshotRequest(
        mode=AssessmentMode.bootstrap,
        bootstrap_key=bootstrap_key,
        ticker=ticker,
        kind=QualityAssetKind.stock,
        venue="BVMF",
        period_at=period_at or (datetime.now(UTC) - timedelta(seconds=1)),
    )


class _BuildService(AssessmentSnapshotService):
    def __init__(self, store: AssessmentStore, *, failed: bool = False) -> None:
        super().__init__(
            store,
            cast(Any, object()),
            cast(Any, object()),
            cast(Any, object()),
            retry_backoff_seconds=2,
        )
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls: list[str] = []
        self.corporate_names: list[str | None] = []
        self.failed = failed

    async def _build(self, request: AssessmentSnapshotRequest) -> AssessmentSnapshotResponse:
        self.calls.append(request.ticker)
        self.corporate_names.append(request.corporate_name)
        self.started.set()
        await self.release.wait()
        status = AssessmentRunStatus.failed if self.failed else AssessmentRunStatus.completed
        components = (
            {
                "assessment": AssessmentComponentState(
                    status=AssessmentComponentStatus.failed,
                    error_code="PROVIDER_UNAVAILABLE",
                    error="provider unavailable",
                    retryable=True,
                )
            }
            if self.failed
            else {}
        )
        return AssessmentSnapshotResponse(
            mode=request.mode,
            bootstrap_key=request.bootstrap_key,
            ticker=request.ticker,
            kind=request.kind,
            venue=request.venue,
            period_at=request.period_at,
            status=status,
            components=components,
            error="provider unavailable" if self.failed else None,
        )


class _WorkerService:
    def __init__(
        self,
        result: tuple[AssessmentSnapshotResponse, bool] | None = None,
    ) -> None:
        self.result = result
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute_claim(
        self,
        _request: AssessmentSnapshotRequest,
        _key: str,
        _claim: ClaimResult,
    ) -> tuple[AssessmentSnapshotResponse, bool]:
        self.started.set()
        if self.result is None:
            await self.release.wait()
            return (
                AssessmentSnapshotResponse(
                    mode=AssessmentMode.bootstrap,
                    bootstrap_key="b" * 64,
                    ticker="TEST3",
                    kind=QualityAssetKind.stock,
                    venue="BVMF",
                    period_at=datetime.now(UTC),
                    status=AssessmentRunStatus.completed,
                ),
                True,
            )
        return self.result

    def _retry_delay(self, _attempts: int) -> int:
        return 1


class _WorkerStore:
    default_lease_seconds = 1

    def __init__(
        self,
        claim: ClaimResult,
        *,
        renew_queue: bool = True,
        renew_snapshot: bool = True,
    ) -> None:
        self.claim_result = claim
        self.renew_queue = renew_queue
        self.renew_snapshot = renew_snapshot
        self.release_claim_calls = 0
        self.release_bootstrap_calls = 0
        self.complete_calls: list[dict[str, Any]] = []
        self.poll_error = False
        self.poll_cancel = False

    async def claim_bootstrap_due(self, **_kwargs: Any) -> list[BootstrapWorkClaim]:
        if self.poll_cancel:
            await asyncio.sleep(10)
        if self.poll_error:
            self.poll_error = False
            raise RuntimeError("poll failed")
        return []

    async def claim(self, **_kwargs: Any) -> ClaimResult:
        return self.claim_result

    async def release_claim(self, **_kwargs: Any) -> bool:
        self.release_claim_calls += 1
        return True

    async def release_bootstrap(self, **_kwargs: Any) -> bool:
        self.release_bootstrap_calls += 1
        return True

    async def complete_bootstrap(self, **kwargs: Any) -> bool:
        self.complete_calls.append(kwargs)
        return True

    async def renew_bootstrap_lease(self, **_kwargs: Any) -> bool:
        return self.renew_queue

    async def renew_claim(self, **_kwargs: Any) -> bool:
        return self.renew_snapshot


def _worker_work(now: datetime | None = None, *, phase: str = "running") -> BootstrapWork:
    current = now or datetime.now(UTC)
    return BootstrapWork(
        key="assessment:7:bootstrap:TEST3:stock:BVMF:" + "b" * 64,
        bootstrap_key="b" * 64,
        ticker="TEST3",
        kind="stock",
        venue="BVMF",
        corporate_name="Issuer",
        period_at=current,
        phase=phase,
        lease_token="queue-token",
        lease_expires_at=current + timedelta(seconds=30),
        retry_at=None,
        error=None,
        created_at=current,
        updated_at=current,
    )


def _worker_record(
    now: datetime | None = None,
    *,
    status: AssessmentRunStatus = AssessmentRunStatus.processing,
    retry_at: datetime | None = None,
    lease_expires_at: datetime | None = None,
    payload: dict[str, Any] | None = None,
) -> AssessmentRecord:
    current = now or datetime.now(UTC)
    return AssessmentRecord(
        key="assessment:7:bootstrap:TEST3:stock:BVMF:" + "b" * 64,
        ticker="TEST3",
        kind="stock",
        profile=None,
        venue="BVMF",
        period_at=current,
        status=status,
        attempts=1,
        generation=1,
        lease_token="snapshot-token",
        lease_expires_at=lease_expires_at,
        retry_at=retry_at,
        payload=payload,
        error="provider failed" if status is AssessmentRunStatus.failed else None,
        evidence_digest=None,
        fetched_at=None,
    )


def _worker_claim(
    state: str,
    record: AssessmentRecord,
    *,
    token: str | None = "snapshot-token",
    generation: int | None = 1,
) -> ClaimResult:
    return ClaimResult(state=state, record=record, token=token, generation=generation)


async def _run_worker_once(
    store: _WorkerStore,
    service: _WorkerService,
    claim: BootstrapWorkClaim,
) -> BootstrapAssessmentWorker:
    worker = BootstrapAssessmentWorker(
        cast(Any, store),
        cast(Any, service),
        lease_seconds=1,
        retry_after_seconds=1,
        poll_interval_seconds=0.05,
    )
    await worker._process(claim)
    return worker


async def _worker(
    store: AssessmentStore,
    service: AssessmentSnapshotService,
) -> BootstrapAssessmentWorker:
    worker = BootstrapAssessmentWorker(
        store,
        service,
        concurrency=2,
        poll_interval_seconds=0.01,
        lease_seconds=30,
        retry_after_seconds=1,
    )
    service.set_bootstrap_wakeup(worker.wake)
    await worker.start()
    return worker


def test_bootstrap_identity_validation_and_scheduler_compatibility() -> None:
    with pytest.raises(ValidationError):
        AssessmentSnapshotRequest(
            mode=AssessmentMode.bootstrap,
            bootstrap_key="A" * 64,
            ticker="TEST3",
            kind=QualityAssetKind.stock,
            period_at=datetime.now(UTC),
        )
    bootstrap = _request()
    assert bootstrap.period_at.tzinfo is not None
    assert len(bootstrap_assessment_key(bootstrap).split(":")[-1]) == 64
    with pytest.raises(ValidationError):
        AssessmentSnapshotRequest(
            ticker="TEST3",
            kind=QualityAssetKind.stock,
            period_at=datetime.now(UTC),
        )
    with pytest.raises(ValidationError):
        AssessmentSnapshotRequest(
            mode=AssessmentMode.bootstrap,
            ticker="TEST3",
            kind=QualityAssetKind.stock,
            period_at=datetime.now(UTC),
        )
    with pytest.raises(ValidationError):
        AssessmentSnapshotRequest(
            ticker="TEST3",
            kind=QualityAssetKind.stock,
            bootstrap_key="b" * 64,
            period_at=datetime.now(ZoneInfo("America/Sao_Paulo")).replace(
                hour=12, minute=0, second=0, microsecond=0
            ),
        )


@pytest.mark.asyncio
async def test_bootstrap_service_terminal_states_and_period_validation(tmp_path: Path) -> None:
    store = AssessmentStore(tmp_path / "assessment.sqlite3")
    await store.startup()
    service = _BuildService(store)
    request = _request()
    now = datetime.now(UTC)
    try:
        with pytest.raises(InvalidAssessmentPeriodError, match="timezone"):
            service._validate_bootstrap_period(datetime.now())
        with pytest.raises(InvalidAssessmentPeriodError, match="future"):
            service._validate_bootstrap_period(now + timedelta(minutes=1))
        with pytest.raises(InvalidAssessmentPeriodError, match="retention"):
            service._validate_bootstrap_period(
                now - timedelta(days=service.period_history_days + 2)
            )
        with pytest.raises(InvalidAssessmentPeriodError, match="scheduler window"):
            service._validate_period(
                (now - timedelta(days=1))
                .astimezone(ASSESSMENT_TIMEZONE)
                .replace(hour=12, minute=30, second=0, microsecond=0)
            )

        key = bootstrap_assessment_key(request)
        admitted = await store.enqueue_bootstrap(
            key=key,
            bootstrap_key=request.bootstrap_key or "",
            ticker=request.ticker,
            kind=request.kind.value,
            venue=request.venue,
            period_at=request.period_at,
            now=now,
        )
        async with store._lock:
            db = store._require_db()
            await db.execute(
                f"UPDATE {assessment_store.ASSESSMENT_BOOTSTRAP_TABLE} "
                "SET phase = 'failed', error = 'failed', retry_at = NULL WHERE run_key = ?",
                (key,),
            )
            await db.commit()
        failed = await service.resolve(request)
        assert failed.status is AssessmentRunStatus.failed

        async with store._lock:
            db = store._require_db()
            await db.execute(
                f"UPDATE {assessment_store.ASSESSMENT_BOOTSTRAP_TABLE} "
                "SET phase = 'failed', error = 'waiting', retry_at = ? WHERE run_key = ?",
                ((now + timedelta(seconds=20)).isoformat(), key),
            )
            await db.commit()
        waiting = await service.resolve(request)
        assert waiting.status is AssessmentRunStatus.failed
        assert waiting.retry_after_seconds is not None

        assert admitted.work.period_at == request.period_at
        assert assessment_key(request) == key
        with pytest.raises(ValueError, match="bootstrap_key"):
            bootstrap_assessment_key(request.model_copy(update={"bootstrap_key": None}))
        processing = service._bootstrap_response(request, None)
        assert processing.status is AssessmentRunStatus.processing

        payload = processing.model_copy(
            update={"status": AssessmentRunStatus.completed}
        ).model_dump(mode="json")
        processing_record = _worker_record(now, payload=payload)
        claimed_response = service._claim_response(
            request,
            ClaimResult("in_progress", processing_record),
        )
        assert claimed_response.status is AssessmentRunStatus.processing
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_service_fenced_publish_returns_winner(tmp_path: Path) -> None:
    store = AssessmentStore(tmp_path / "assessment.sqlite3")
    await store.startup()
    service = _BuildService(store)
    request = _request(period_at=datetime.now(UTC) - timedelta(seconds=2))
    key = bootstrap_assessment_key(request)
    try:
        claim = await store.claim(
            key=key,
            ticker=request.ticker,
            kind=request.kind.value,
            profile=None,
            venue=request.venue,
            period_at=request.period_at,
        )
        assert claim.state == "claimed"
        assert claim.token is not None and claim.generation is not None
        winner = AssessmentSnapshotResponse(
            mode=AssessmentMode.bootstrap,
            bootstrap_key=request.bootstrap_key,
            ticker=request.ticker,
            kind=request.kind,
            venue=request.venue,
            period_at=request.period_at,
            status=AssessmentRunStatus.completed,
        )
        service.release.set()

        async def stale_publish(**_kwargs: Any) -> bool:
            return False

        async def latest(_key: str) -> AssessmentRecord | None:
            return _worker_record(datetime.now(UTC), payload=winner.model_dump(mode="json"))

        store.publish = stale_publish  # type: ignore[method-assign]
        store.get = latest  # type: ignore[assignment]
        response, committed = await service.execute_claim(request, key, claim)
        assert committed is False
        assert response.status is AssessmentRunStatus.completed
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_sqlite_bootstrap_queue_leases_and_retry_transitions(tmp_path: Path) -> None:
    now = datetime.now(UTC) - timedelta(seconds=2)
    store = AssessmentStore(tmp_path / "assessment.sqlite3")
    await store.startup()
    request = _request(period_at=now)
    key = bootstrap_assessment_key(request)
    try:
        await store.enqueue_bootstrap(
            key=key,
            bootstrap_key=request.bootstrap_key or "",
            ticker=request.ticker,
            kind=request.kind.value,
            venue=request.venue,
            period_at=request.period_at,
            now=now,
        )
        queue_claim = (await store.claim_bootstrap_due(limit=1, now=now))[0]
        assert await store.renew_bootstrap_lease(
            key=key,
            token=queue_claim.token,
            now=now,
            lease_seconds=30,
        )
        assert await store.release_bootstrap(key=key, token=queue_claim.token, now=now)
        queue_claim = (await store.claim_bootstrap_due(limit=1, now=now))[0]
        assert await store.complete_bootstrap(
            key=key,
            token=queue_claim.token,
            success=False,
            retry_after_seconds=2,
            error="temporary",
            now=now,
        )
        async with store._lock:
            db = store._require_db()
            await db.execute(
                f"UPDATE {assessment_store.ASSESSMENT_BOOTSTRAP_TABLE} "
                "SET retry_at = ? WHERE run_key = ?",
                ((now - timedelta(seconds=1)).isoformat(), key),
            )
            await db.commit()
        queue_claim = (await store.claim_bootstrap_due(limit=1, now=now))[0]
        assert queue_claim.work.phase == "running"

        snapshot_claim = await store.claim(
            key=key,
            ticker=request.ticker,
            kind=request.kind.value,
            profile=None,
            venue=request.venue,
            period_at=request.period_at,
            now=now,
        )
        assert snapshot_claim.token is not None and snapshot_claim.generation is not None
        assert await store.renew_claim(
            key=key,
            token=snapshot_claim.token,
            generation=snapshot_claim.generation,
            now=now,
            lease_seconds=30,
        )
        assert await store.release_claim(
            key=key,
            token=snapshot_claim.token,
            generation=snapshot_claim.generation,
            now=now,
        )

        async with store._lock:
            db = store._require_db()
            await db.execute(
                f"UPDATE {assessment_store.ASSESSMENT_BOOTSTRAP_TABLE} "
                "SET phase = 'running', lease_token = 'expired', lease_expires_at = ? "
                "WHERE run_key = ?",
                ((now - timedelta(seconds=1)).isoformat(), key),
            )
            await db.commit()
        admitted = await store.enqueue_bootstrap(
            key=key,
            bootstrap_key=request.bootstrap_key or "",
            ticker=request.ticker,
            kind=request.kind.value,
            venue=request.venue,
            period_at=request.period_at,
            now=now,
        )
        assert admitted.state == "queued"
        async with store._lock:
            db = store._require_db()
            await db.execute(
                f"UPDATE {assessment_store.ASSESSMENT_BOOTSTRAP_TABLE} "
                "SET phase = 'failed', retry_at = NULL WHERE run_key = ?",
                (key,),
            )
            await db.commit()
        terminal = await store.enqueue_bootstrap(
            key=key,
            bootstrap_key=request.bootstrap_key or "",
            ticker=request.ticker,
            kind=request.kind.value,
            venue=request.venue,
            period_at=request.period_at,
            now=now,
        )
        assert terminal.state == "failed"
        expired = replace(
            _worker_work(now),
            phase="running",
            lease_expires_at=now - timedelta(seconds=1),
        )
        queued, queued_state = _prepare_bootstrap_work(expired, now=now)
        assert queued.phase == "queued" and queued_state == "queued"
        retried = replace(
            _worker_work(now),
            phase="failed",
            retry_at=now - timedelta(seconds=1),
        )
        retried_work, retried_state = _prepare_bootstrap_work(retried, now=now)
        assert retried_work.phase == "queued" and retried_state == "queued"
        with pytest.raises(assessment_store.AssessmentStoreError, match="Unknown bootstrap"):
            _prepare_bootstrap_work(replace(_worker_work(now), phase="unexpected"), now=now)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_simultaneous_bootstrap_requests_build_once_and_cache(
    tmp_path: Path,
) -> None:
    store = AssessmentStore(tmp_path / "assessment.sqlite3", default_lease_seconds=30)
    await store.startup()
    service = _BuildService(store)
    worker = await _worker(store, service)
    request = _request()
    first_task = asyncio.create_task(service.resolve(request))
    await asyncio.wait_for(service.started.wait(), timeout=1)
    second = await service.resolve(request)
    assert second.status is AssessmentRunStatus.processing
    service.release.set()
    first = await first_task
    assert first.status is AssessmentRunStatus.processing
    for _ in range(100):
        cached = await service.resolve(request)
        if cached.status is AssessmentRunStatus.completed:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("bootstrap worker did not publish the snapshot")
    assert cached.bootstrap_key == request.bootstrap_key
    assert cached.period_at == request.period_at
    assert service.calls == ["TEST3"]
    await worker.stop()
    await store.close()


@pytest.mark.asyncio
async def test_bootstrap_preserves_first_corporate_name_hint(tmp_path: Path) -> None:
    store = AssessmentStore(tmp_path / "assessment.sqlite3")
    await store.startup()
    service = _BuildService(store)
    worker = await _worker(store, service)
    request = _request().model_copy(update={"corporate_name": "Canonical Issuer"})
    await service.resolve(request)
    later = request.model_copy(update={"corporate_name": "Other Issuer"})
    await service.resolve(later)
    service.release.set()
    for _ in range(100):
        if service.calls:
            break
        await asyncio.sleep(0.01)
    assert service.corporate_names == ["Canonical Issuer"]
    await worker.stop()
    await store.close()


@pytest.mark.asyncio
async def test_bootstrap_period_mismatch_is_rejected_without_identity_leak(tmp_path: Path) -> None:
    store = AssessmentStore(tmp_path / "assessment.sqlite3")
    await store.startup()
    service = _BuildService(store)
    first = _request(period_at=datetime.now(UTC) - timedelta(seconds=2))
    await service.resolve(first)
    mismatched = first.model_copy(update={"period_at": first.period_at - timedelta(seconds=1)})
    with pytest.raises(Exception, match="different period"):
        await service.resolve(mismatched)
    await store.close()


@pytest.mark.asyncio
async def test_bootstrap_work_survives_store_restart(tmp_path: Path) -> None:
    path = tmp_path / "assessment.sqlite3"
    request = _request()
    store = AssessmentStore(path)
    await store.startup()
    service = _BuildService(store)
    assert (await service.resolve(request)).status is AssessmentRunStatus.processing
    await store.close()

    store = AssessmentStore(path)
    await store.startup()
    service = _BuildService(store)
    worker = await _worker(store, service)
    service.release.set()
    for _ in range(100):
        result = await service.resolve(request)
        if result.status is AssessmentRunStatus.completed:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("restarted bootstrap worker did not publish the snapshot")
    assert service.calls == ["TEST3"]
    await worker.stop()
    await store.close()


@pytest.mark.asyncio
async def test_bootstrap_provider_failure_keeps_backoff_and_attempt_budget(tmp_path: Path) -> None:
    store = AssessmentStore(tmp_path / "assessment.sqlite3", max_attempts=2)
    await store.startup()
    service = _BuildService(store, failed=True)
    worker = await _worker(store, service)
    request = _request()
    assert (await service.resolve(request)).status is AssessmentRunStatus.processing
    await asyncio.wait_for(service.started.wait(), timeout=1)
    service.release.set()
    for _ in range(100):
        record = await store.get(bootstrap_assessment_key(request))
        if record is not None and record.status is AssessmentRunStatus.failed:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("failed bootstrap was not persisted")
    retry = await service.resolve(request)
    assert retry.status is AssessmentRunStatus.failed
    assert retry.retry_after_seconds is not None and retry.retry_after_seconds > 0
    assert record.attempts == 1
    assert service.calls == ["TEST3"]
    await worker.stop()
    await store.close()


@pytest.mark.asyncio
async def test_bootstrap_build_timeout_cancels_provider_and_requeues(tmp_path: Path) -> None:
    store = AssessmentStore(tmp_path / "assessment.sqlite3", default_lease_seconds=1)
    await store.startup()
    service = _BuildService(store)
    worker = BootstrapAssessmentWorker(
        store,
        service,
        poll_interval_seconds=0.01,
        lease_seconds=1,
        build_timeout_seconds=0.05,
        retry_after_seconds=1,
    )
    service.set_bootstrap_wakeup(worker.wake)
    await worker.start()
    request = _request()
    await service.resolve(request)
    await asyncio.wait_for(service.started.wait(), timeout=1)
    for _ in range(100):
        record = await store.get(bootstrap_assessment_key(request))
        if record is not None and record.error == "cancelled":
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("timed-out bootstrap claim was not fenced")
    assert not service.release.is_set()
    await worker.stop()
    await store.close()


@pytest.mark.asyncio
async def test_worker_poll_error_and_idempotent_start_stop() -> None:
    now = datetime.now(UTC)
    claim = _worker_claim("in_progress", _worker_record(now))
    store = _WorkerStore(claim)
    store.poll_error = True
    worker = BootstrapAssessmentWorker(
        cast(Any, store),
        cast(Any, _WorkerService()),
        poll_interval_seconds=0.05,
    )
    await worker.start()
    await worker.start()
    await asyncio.sleep(0.08)
    await worker.stop()
    assert store.poll_error is False


@pytest.mark.asyncio
async def test_worker_stop_cancels_poll_and_active_job() -> None:
    now = datetime.now(UTC)
    store = _WorkerStore(_worker_claim("in_progress", _worker_record(now)))
    store.poll_cancel = True
    worker = BootstrapAssessmentWorker(
        cast(Any, store),
        cast(Any, _WorkerService()),
        poll_interval_seconds=0.05,
    )
    active_job = asyncio.create_task(asyncio.sleep(10))
    worker._jobs.add(active_job)
    await worker.start()
    await asyncio.sleep(0)
    await worker.stop()
    assert active_job.cancelled()


@pytest.mark.asyncio
async def test_worker_lease_loss_without_renewal_error_and_generic_failure() -> None:
    now = datetime.now(UTC)
    claim = _worker_claim("claimed", _worker_record(now))
    store = _WorkerStore(claim)
    service = _WorkerService()
    worker = BootstrapAssessmentWorker(cast(Any, store), cast(Any, service), lease_seconds=1)

    async def renew_without_error(*_args: Any, **_kwargs: Any) -> None:
        return None

    worker._renew_leases = renew_without_error  # type: ignore[method-assign]
    await worker._process(BootstrapWorkClaim(_worker_work(now), "queue-token"))
    assert store.release_bootstrap_calls == 1

    class _RaisingService(_WorkerService):
        async def execute_claim(
            self,
            _request: AssessmentSnapshotRequest,
            _key: str,
            _claim: ClaimResult,
        ) -> tuple[AssessmentSnapshotResponse, bool]:
            self.started.set()
            raise RuntimeError("provider exploded")

    failed_store = _WorkerStore(claim)
    await BootstrapAssessmentWorker(
        cast(Any, failed_store),
        cast(Any, _RaisingService()),
        lease_seconds=1,
    )._process(BootstrapWorkClaim(_worker_work(now), "queue-token"))
    assert failed_store.complete_calls[-1]["success"] is False


@pytest.mark.asyncio
async def test_worker_handles_claim_states_and_missing_tokens() -> None:
    now = datetime.now(UTC)
    work = _worker_work(now)
    queue_claim = BootstrapWorkClaim(work=work, token="queue-token")

    missing = _WorkerStore(_worker_claim("claimed", _worker_record(now), token=None))
    await _run_worker_once(missing, _WorkerService(), queue_claim)
    assert missing.release_bootstrap_calls == 1

    existing_payload = AssessmentSnapshotResponse(
        mode=AssessmentMode.bootstrap,
        bootstrap_key=work.bootstrap_key,
        ticker="TEST3",
        kind=QualityAssetKind.stock,
        venue="BVMF",
        period_at=now,
        status=AssessmentRunStatus.completed,
    ).model_dump(mode="json")
    existing = _WorkerStore(
        _worker_claim(
            "existing",
            _worker_record(now, status=AssessmentRunStatus.completed, payload=existing_payload),
            token=None,
            generation=None,
        )
    )
    await _run_worker_once(existing, _WorkerService(), queue_claim)
    assert existing.complete_calls and existing.complete_calls[-1]["success"] is True

    in_progress = _WorkerStore(
        _worker_claim(
            "in_progress",
            _worker_record(now, retry_at=now + timedelta(seconds=5)),
            token=None,
            generation=None,
        )
    )
    await _run_worker_once(in_progress, _WorkerService(), queue_claim)
    assert in_progress.complete_calls[-1]["retry_after_seconds"] >= 1

    exhausted = _WorkerStore(
        _worker_claim(
            "retry_exhausted",
            _worker_record(now, status=AssessmentRunStatus.failed),
            token=None,
            generation=None,
        )
    )
    await _run_worker_once(exhausted, _WorkerService(), queue_claim)
    assert exhausted.complete_calls[-1]["success"] is False

    existing_processing = _WorkerStore(
        _worker_claim(
            "existing",
            _worker_record(now, status=AssessmentRunStatus.processing),
            token=None,
            generation=None,
        )
    )
    await _run_worker_once(existing_processing, _WorkerService(), queue_claim)
    assert existing_processing.release_bootstrap_calls == 1


@pytest.mark.asyncio
async def test_worker_lease_loss_cancels_provider_and_fences_claim() -> None:
    now = datetime.now(UTC)
    claim = _worker_claim("claimed", _worker_record(now))
    store = _WorkerStore(claim, renew_queue=False)
    service = _WorkerService()
    worker = BootstrapAssessmentWorker(
        cast(Any, store),
        cast(Any, service),
        lease_seconds=1,
        poll_interval_seconds=0.05,
    )
    await worker._process(BootstrapWorkClaim(_worker_work(now), "queue-token"))
    assert service.started.is_set()
    assert store.release_claim_calls == 1
    assert store.release_bootstrap_calls == 1


@pytest.mark.asyncio
async def test_worker_committed_false_releases_queue_and_cancellation_logs_cleanup_failure() -> (
    None
):
    now = datetime.now(UTC)
    record = _worker_record(now)
    response = AssessmentSnapshotResponse(
        mode=AssessmentMode.bootstrap,
        bootstrap_key="b" * 64,
        ticker="TEST3",
        kind=QualityAssetKind.stock,
        venue="BVMF",
        period_at=now,
        status=AssessmentRunStatus.processing,
    )
    store = _WorkerStore(_worker_claim("claimed", record))
    worker = BootstrapAssessmentWorker(
        cast(Any, store), cast(Any, _WorkerService((response, False)))
    )
    await worker._process(BootstrapWorkClaim(_worker_work(now), "queue-token"))
    assert store.release_bootstrap_calls == 1

    class _FailingCleanupStore(_WorkerStore):
        async def release_claim(self, **_kwargs: Any) -> bool:
            raise RuntimeError("cleanup claim failed")

        async def release_bootstrap(self, **_kwargs: Any) -> bool:
            raise RuntimeError("cleanup queue failed")

        async def complete_bootstrap(self, **_kwargs: Any) -> bool:
            raise RuntimeError("completion queue failed")

    failing = _FailingCleanupStore(_worker_claim("claimed", record))
    hanging = _WorkerService()
    worker = BootstrapAssessmentWorker(cast(Any, failing), cast(Any, hanging), lease_seconds=1)
    task = asyncio.create_task(
        worker._process(BootstrapWorkClaim(_worker_work(now), "queue-token"))
    )
    await asyncio.wait_for(hanging.started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await worker._safe_complete_bootstrap(
        key=_worker_work(now).key,
        token="queue-token",
        success=False,
        error="cleanup",
    )


@pytest.mark.asyncio
async def test_worker_renew_and_retry_helpers_cover_fences() -> None:
    now = datetime.now(UTC)
    claim = _worker_claim("claimed", _worker_record(now))
    store = _WorkerStore(claim)
    worker = BootstrapAssessmentWorker(
        cast(Any, store), cast(Any, _WorkerService()), lease_seconds=1
    )
    assert worker._claim_retry_after(_worker_claim("in_progress", _worker_record(now))) == 2
    assert (
        worker._claim_retry_after(
            _worker_claim(
                "in_progress",
                _worker_record(now, lease_expires_at=now + timedelta(seconds=5)),
            )
        )
        >= 1
    )
    assert (
        worker._claim_retry_after(
            _worker_claim("in_progress", _worker_record(now, lease_expires_at=None))
        )
        == 2
    )
    await worker._safe_release_claim(_worker_work(now), None)
    await worker._safe_release_claim(
        _worker_work(now), _worker_claim("claimed", _worker_record(now), token=None)
    )
