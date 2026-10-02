from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from app.assessment.models import AssessmentMode, AssessmentRunStatus, AssessmentSnapshotRequest
from app.assessment.service import AssessmentSnapshotService
from app.assessment.store import AssessmentStore, BootstrapWork, BootstrapWorkClaim, ClaimResult
from app.models.quality import QualityAssetKind

_LOGGER = logging.getLogger(__name__)


class _LeaseLostError(RuntimeError):
    """Internal signal used to cancel provider I/O after fencing."""


class BootstrapAssessmentWorker:
    """Lifecycle-managed durable worker for asynchronous bootstrap snapshots.

    Queue admission happens in the HTTP service, while all provider I/O runs
    here.  Queue and provider leases are renewed independently, so a long CVM
    archive request remains fenced to one owner without making a crashed
    process block recovery for the full build budget.
    """

    def __init__(
        self,
        store: AssessmentStore,
        service: AssessmentSnapshotService,
        *,
        concurrency: int = 2,
        poll_interval_seconds: float = 1.0,
        lease_seconds: int | None = None,
        retry_after_seconds: int = 2,
        build_timeout_seconds: float = 1200.0,
    ) -> None:
        self.store = store
        self.service = service
        self.concurrency = max(1, concurrency)
        self.poll_interval_seconds = max(0.05, min(60.0, poll_interval_seconds))
        self.lease_seconds = lease_seconds
        self.retry_after_seconds = max(1, min(60, retry_after_seconds))
        self.build_timeout_seconds = max(0.05, min(3600.0, build_timeout_seconds))
        self._wake_event = asyncio.Event()
        self._stopping = False
        self._task: asyncio.Task[None] | None = None
        self._jobs: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="assessment-bootstrap-worker")

    async def stop(self) -> None:
        self._stopping = True
        self._wake_event.set()
        task = self._task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._task = None
        jobs = tuple(self._jobs)
        for job in jobs:
            job.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
        self._jobs.clear()

    def wake(self) -> None:
        """Wake polling immediately after a durable enqueue."""

        self._wake_event.set()

    async def _run(self) -> None:
        while not self._stopping:
            capacity = self.concurrency - len(self._jobs)
            if capacity > 0:
                try:
                    claims = await self.store.claim_bootstrap_due(
                        limit=capacity,
                        lease_seconds=self.lease_seconds,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    _LOGGER.warning(
                        "assessment bootstrap poll failed",
                        extra={"error_type": type(exc).__name__},
                    )
                    claims = []
                for claim in claims:
                    job = asyncio.create_task(self._process(claim))
                    self._jobs.add(job)
                    job.add_done_callback(self._jobs.discard)
                if claims:
                    await asyncio.sleep(0)
                    continue
            self._wake_event.clear()
            try:
                await asyncio.wait_for(self._wake_event.wait(), self.poll_interval_seconds)
            except TimeoutError:
                continue

    async def _process(self, queue_claim: BootstrapWorkClaim) -> None:
        work = queue_claim.work
        snapshot_claim: ClaimResult | None = None
        renew_task: asyncio.Task[None] | None = None
        build_task: asyncio.Task[Any] | None = None
        try:
            request = AssessmentSnapshotRequest(
                mode=AssessmentMode.bootstrap,
                bootstrap_key=work.bootstrap_key,
                ticker=work.ticker,
                kind=QualityAssetKind(work.kind),
                venue=work.venue,
                corporate_name=work.corporate_name,
                period_at=work.period_at,
            )
            snapshot_claim = await self.store.claim(
                key=work.key,
                ticker=work.ticker,
                kind=work.kind,
                profile=None,
                venue=work.venue,
                period_at=work.period_at,
                lease_seconds=self.lease_seconds,
            )
            if snapshot_claim.state == "claimed":
                if snapshot_claim.token is None or snapshot_claim.generation is None:
                    await self._safe_release_bootstrap(work, queue_claim.token)
                    return
                renew_task = asyncio.create_task(
                    self._renew_leases(
                        work.key,
                        queue_claim.token,
                        snapshot_claim.token,
                        snapshot_claim.generation,
                    )
                )
                build_task = asyncio.create_task(
                    self.service.execute_claim(request, work.key, snapshot_claim)
                )
                done, _pending = await asyncio.wait(
                    {build_task, renew_task},
                    timeout=self.build_timeout_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    build_task.cancel()
                    await asyncio.gather(build_task, return_exceptions=True)
                    raise TimeoutError("bootstrap assessment build timed out")
                if renew_task in done:
                    renewal_error = renew_task.exception()
                    build_task.cancel()
                    await asyncio.gather(build_task, return_exceptions=True)
                    if renewal_error is not None:
                        raise renewal_error
                    raise _LeaseLostError("bootstrap assessment lease renewal stopped")
                result, committed = await build_task
                if not committed:
                    await self._safe_release_bootstrap(work, queue_claim.token)
                    return
                if result.status is AssessmentRunStatus.completed:
                    await self._safe_complete_bootstrap(
                        key=work.key,
                        token=queue_claim.token,
                        success=True,
                    )
                else:
                    await self._safe_complete_bootstrap(
                        key=work.key,
                        token=queue_claim.token,
                        success=False,
                        retry_after_seconds=self.service._retry_delay(
                            snapshot_claim.record.attempts
                        ),
                        error=result.error,
                    )
                return

            if snapshot_claim.state == "existing":
                existing = snapshot_claim.record.response()
                if existing is not None and existing.status is AssessmentRunStatus.completed:
                    await self._safe_complete_bootstrap(
                        key=work.key,
                        token=queue_claim.token,
                        success=True,
                    )
                else:
                    await self._safe_release_bootstrap(work, queue_claim.token)
                return

            if snapshot_claim.state == "retry_exhausted":
                await self._safe_complete_bootstrap(
                    key=work.key,
                    token=queue_claim.token,
                    success=False,
                    error=snapshot_claim.record.error or "retry_exhausted",
                )
                return

            # A different worker may still own the provider lease, or the
            # provider failure backoff may not have elapsed.  Keep this queue
            # item durable and let a short poll retry without consuming an
            # additional build attempt.
            await self._safe_complete_bootstrap(
                key=work.key,
                token=queue_claim.token,
                success=False,
                retry_after_seconds=self._claim_retry_after(snapshot_claim),
                error=snapshot_claim.record.error,
            )
        except asyncio.CancelledError:
            if build_task is not None:
                build_task.cancel()
                await asyncio.gather(build_task, return_exceptions=True)
            await self._safe_release_claim(work, snapshot_claim)
            await self._safe_release_bootstrap(work, queue_claim.token)
            raise
        except TimeoutError:
            await self._safe_release_claim(work, snapshot_claim)
            _LOGGER.warning(
                "assessment bootstrap build timed out",
                extra={"error_type": "TimeoutError", "run_key": work.key},
            )
            await self._safe_complete_bootstrap(
                key=work.key,
                token=queue_claim.token,
                success=False,
                retry_after_seconds=self.retry_after_seconds,
                error="Bootstrap assessment build timed out.",
            )
        except _LeaseLostError:
            await self._safe_release_claim(work, snapshot_claim)
            _LOGGER.warning(
                "assessment bootstrap lease was fenced during build",
                extra={"error_type": "LeaseLost", "run_key": work.key},
            )
            await self._safe_release_bootstrap(work, queue_claim.token)
        except Exception as exc:
            _LOGGER.warning(
                "assessment bootstrap job failed before completion",
                extra={"error_type": type(exc).__name__, "run_key": work.key},
            )
            await self._safe_release_claim(work, snapshot_claim)
            await self._safe_complete_bootstrap(
                key=work.key,
                token=queue_claim.token,
                success=False,
                retry_after_seconds=self.retry_after_seconds,
                error="Bootstrap worker failed before publishing a result.",
            )
        finally:
            if renew_task is not None:
                renew_task.cancel()
                await asyncio.gather(renew_task, return_exceptions=True)

    async def _release_snapshot_claim(
        self,
        work: BootstrapWork,
        snapshot_claim: ClaimResult | None,
    ) -> None:
        if snapshot_claim is None:
            return
        if snapshot_claim.token is None or snapshot_claim.generation is None:
            return
        await self.store.release_claim(
            key=work.key,
            token=snapshot_claim.token,
            generation=snapshot_claim.generation,
        )

    async def _safe_release_claim(
        self,
        work: BootstrapWork,
        snapshot_claim: ClaimResult | None,
    ) -> None:
        """Fence a cancelled provider task without masking cancellation."""

        try:
            await self._release_snapshot_claim(work, snapshot_claim)
        except Exception as exc:
            _LOGGER.warning(
                "assessment bootstrap claim cleanup failed",
                extra={"error_type": type(exc).__name__, "run_key": work.key},
            )

    async def _safe_release_bootstrap(self, work: BootstrapWork, token: str) -> None:
        """Return a cancelled queue lease while preserving task cancellation."""

        try:
            await self.store.release_bootstrap(key=work.key, token=token)
        except Exception as exc:
            _LOGGER.warning(
                "assessment bootstrap queue cleanup failed",
                extra={"error_type": type(exc).__name__, "run_key": work.key},
            )

    async def _safe_complete_bootstrap(
        self,
        *,
        key: str,
        token: str,
        success: bool,
        retry_after_seconds: int = 0,
        error: str | None = None,
    ) -> None:
        """Persist queue completion while keeping worker tasks recoverable."""

        try:
            await self.store.complete_bootstrap(
                key=key,
                token=token,
                success=success,
                retry_after_seconds=retry_after_seconds,
                error=error,
            )
        except Exception as exc:
            _LOGGER.warning(
                "assessment bootstrap queue completion failed",
                extra={"error_type": type(exc).__name__, "run_key": key},
            )

    async def _renew_leases(
        self,
        key: str,
        queue_token: str,
        snapshot_token: str,
        generation: int,
    ) -> None:
        lease_seconds = self.lease_seconds or self.store.default_lease_seconds
        interval = max(0.25, min(60.0, lease_seconds / 3))
        while True:
            await asyncio.sleep(interval)
            now = datetime.now(UTC)
            queue_renewed = await self.store.renew_bootstrap_lease(
                key=key,
                token=queue_token,
                now=now,
                lease_seconds=lease_seconds,
            )
            claim_renewed = await self.store.renew_claim(
                key=key,
                token=snapshot_token,
                generation=generation,
                now=now,
                lease_seconds=lease_seconds,
            )
            if not queue_renewed or not claim_renewed:
                raise _LeaseLostError("bootstrap assessment lease renewal failed")

    def _claim_retry_after(self, claim: ClaimResult) -> int:
        record = claim.record
        if record.retry_at is not None:
            remaining = int((record.retry_at - datetime.now(UTC)).total_seconds())
            return max(1, min(3600, remaining))
        if record.lease_expires_at is not None:
            remaining = int((record.lease_expires_at - datetime.now(UTC)).total_seconds())
            return max(1, min(3600, remaining))
        return self.retry_after_seconds


__all__ = ["BootstrapAssessmentWorker"]
