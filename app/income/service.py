from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from secrets import token_hex

from app.income.resolver import resolve_income_events
from app.income.sources import IncomeSource, IncomeSourceResult
from app.income.store import (
    REFRESH_COMPLETED,
    REFRESH_PARTIAL,
    IncomeEventStore,
)
from app.models.income_events import (
    IncomeEventAsyncRefreshResponse,
    IncomeEventBatchRequest,
    IncomeEventBatchResponse,
    IncomeEventChangesResponse,
    IncomeEventCoverageItem,
    IncomeEventCoverageResponse,
    IncomeEventRefreshRequest,
    IncomeEventRefreshResponse,
    IncomeInstrumentRequest,
    IncomeSourceCoverage,
)

_LOGGER = logging.getLogger(__name__)


class IncomeEventService:
    def __init__(
        self,
        store: IncomeEventStore,
        sources: list[IncomeSource],
        *,
        snapshot_overlap_days: int = 365,
        refresh_ttl_seconds: int = 0,
        job_batch_size: int = 32,
        job_lease_seconds: int = 120,
        job_max_attempts: int = 3,
        worker_poll_seconds: float = 0.5,
    ) -> None:
        self.store = store
        self.sources = sources
        self.snapshot_overlap_days = snapshot_overlap_days
        self.refresh_ttl_seconds = refresh_ttl_seconds
        self.job_batch_size = max(job_batch_size, 1)
        self.job_lease_seconds = max(job_lease_seconds, 1)
        self.job_max_attempts = max(job_max_attempts, 1)
        self.worker_poll_seconds = max(worker_poll_seconds, 0.01)
        self._inflight: dict[
            tuple[tuple[tuple[str, str | None], ...], date],
            asyncio.Task[IncomeEventRefreshResponse],
        ] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self._worker_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()

    async def startup(self) -> None:
        """Start the background drain of queued refresh jobs."""
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(self._worker_loop())

    async def refresh(
        self,
        request: IncomeEventRefreshRequest,
    ) -> IncomeEventRefreshResponse:
        instruments = _unique_instruments(request.instruments)
        as_of = request.as_of or date.today()
        identity = tuple(
            sorted(
                ((item.ticker, item.isin) for item in instruments),
                key=lambda item: (item[0], item[1] or ""),
            )
        )
        key = (identity, as_of)
        async with self._lock:
            if self._closed:
                raise RuntimeError("IncomeEventService is closed")
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(self._refresh(instruments, as_of))
                self._inflight[key] = task
                task.add_done_callback(lambda completed: self._cleanup_inflight(key, completed))
        return await asyncio.shield(task)

    async def close(self) -> None:
        """Stop accepting refreshes, drain the worker and shielded producers.

        Refresh waiters may be cancelled independently of the shared producer.
        Waiting for the producer here guarantees that no source task can write
        through a store after the application starts closing that store.
        """

        async with self._lock:
            self._closed = True
        worker = self._worker_task
        self._worker_task = None
        if worker is not None:
            self._stop_event.set()
            self._wake_event.set()
            await asyncio.gather(worker, return_exceptions=True)
        async with self._lock:
            pending = tuple(self._inflight.values())
        if pending:
            await asyncio.gather(
                *(asyncio.shield(task) for task in pending),
                return_exceptions=True,
            )
            await asyncio.sleep(0)

    def _cleanup_inflight(
        self,
        key: tuple[tuple[tuple[str, str | None], ...], date],
        task: asyncio.Future[IncomeEventRefreshResponse],
    ) -> None:
        """Drop only the completed task that still owns its request key."""
        if self._inflight.get(key) is task:
            self._inflight.pop(key, None)
        if not task.cancelled():
            task.exception()

    async def _refresh(
        self,
        instruments: list[IncomeInstrumentRequest],
        as_of: date,
    ) -> IncomeEventRefreshResponse:
        results = await asyncio.gather(
            *(self._collect_source(source, instruments, as_of) for source in self.sources),
            return_exceptions=True,
        )
        observations = 0
        failed_sources: list[str] = []
        for source, result in zip(self.sources, results, strict=True):
            if isinstance(result, BaseException):
                failed_sources.append(source.name)
                continue
            observations += len(result.observations)
            if not await self._persist_source_result(source, result, as_of):
                failed_sources.append(source.name)
        tickers = [item.ticker for item in instruments]
        resolved = resolve_income_events(await self.store.observations(tickers))
        published = await self.store.publish(resolved, scope_tickers=tickers)
        return IncomeEventRefreshResponse(
            requested=len(instruments),
            observations=observations,
            published=published,
            failed_sources=sorted(set(failed_sources)),
            cursor=await self.store.cursor(),
        )

    async def _persist_source_result(
        self,
        source: IncomeSource,
        result: IncomeSourceResult,
        as_of: date,
    ) -> bool:
        """Store one source's observations and coverage; False when partial."""
        complete_tickers = [item.ticker for item in result.coverage if item.complete]
        incomplete_tickers = {item.ticker for item in result.coverage if not item.complete}
        await self.store.replace_observations(
            result.observations,
            snapshot_sources=source.snapshot_sources,
            complete_tickers=complete_tickers,
            snapshot_from=as_of - timedelta(days=self.snapshot_overlap_days),
        )
        await self.store.save_observations(
            [item for item in result.observations if item.ticker in incomplete_tickers]
        )
        await self.store.save_coverage(list(result.coverage))
        return not any(not item.complete for item in result.coverage)

    async def _collect_source(
        self,
        source: IncomeSource,
        instruments: list[IncomeInstrumentRequest],
        as_of: date,
    ) -> IncomeSourceResult:
        stale = await self._stale_instruments(source, instruments)
        if not stale:
            return IncomeSourceResult([], [])
        return await source.collect(stale, as_of)

    async def _stale_instruments(
        self,
        source: IncomeSource,
        instruments: list[IncomeInstrumentRequest],
    ) -> list[IncomeInstrumentRequest]:
        if self.refresh_ttl_seconds <= 0:
            return list(instruments)
        fresh = await self.store.fresh_coverage(
            source.name,
            [item.ticker for item in instruments],
            not_before=datetime.now(UTC) - timedelta(seconds=self.refresh_ttl_seconds),
        )
        return [item for item in instruments if item.ticker not in fresh]

    async def refresh_async(
        self,
        request: IncomeEventRefreshRequest,
    ) -> IncomeEventAsyncRefreshResponse:
        """Persist a durable refresh job and wake the background worker."""
        instruments = _unique_instruments(request.instruments)
        as_of = request.as_of or date.today()
        items: list[tuple[str, str]] = []
        for source in self.sources:
            items.extend(
                (source.name, item.ticker)
                for item in await self._stale_instruments(source, instruments)
            )
        job_id = token_hex(12)
        now = datetime.now(UTC)
        queued = await self.store.create_refresh_job(
            job_id,
            items,
            requested=len(instruments),
            as_of=as_of,
            now=now,
        )
        if queued:
            self._wake_event.set()
        else:
            await self.store.finish_refresh_job(
                job_id,
                status=REFRESH_COMPLETED,
                error=None,
                now=now,
            )
        return IncomeEventAsyncRefreshResponse(
            job_id=job_id,
            status="queued" if queued else REFRESH_COMPLETED,
            requested=len(instruments),
            queued=queued,
            deduplicated=len(items) - queued,
        )

    async def refresh_job(self, job_id: str) -> dict[str, object] | None:
        return await self.store.refresh_job(job_id)

    async def coverage(self, tickers: list[str]) -> IncomeEventCoverageResponse:
        return IncomeEventCoverageResponse(
            items=[_coverage_item(item) for item in await self.store.coverage(tickers)]
        )

    async def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                processed = await self.process_pending_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a drain failure must not kill the worker
                _LOGGER.exception("income refresh worker failed")
                processed = 0
            if processed:
                continue
            try:
                await asyncio.wait_for(
                    self._wake_event.wait(),
                    timeout=self.worker_poll_seconds,
                )
            except TimeoutError:
                pass
            self._wake_event.clear()

    async def process_pending_once(self, *, limit: int | None = None) -> int:
        """Claim and process one page of queued items; returns how many ran."""
        if self._closed:
            return 0
        now = datetime.now(UTC)
        items = await self.store.claim_refresh_items(
            limit=limit or self.job_batch_size,
            lease_seconds=self.job_lease_seconds,
            now=now,
        )
        if not items:
            return 0
        groups: dict[tuple[str, str], list[dict[str, object]]] = {}
        for item in items:
            groups.setdefault((str(item["source"]), str(item["as_of"])), []).append(item)
        known_sources = {source.name: source for source in self.sources}
        await asyncio.gather(
            *(
                self._process_item_page(source_name, as_of, rows, known_sources)
                for (source_name, as_of), rows in groups.items()
            ),
            return_exceptions=True,
        )
        for job_id in {str(item["job_id"]) for item in items}:
            await self._publish_finished_job(job_id)
        return len(items)

    async def _process_item_page(
        self,
        source_name: str,
        as_of: str,
        rows: list[dict[str, object]],
        known_sources: dict[str, IncomeSource],
    ) -> None:
        source = known_sources.get(source_name)
        if source is None:
            await self._fail_items(rows, error="unknown source")
            return
        instruments = [IncomeInstrumentRequest(ticker=str(row["ticker"])) for row in rows]
        try:
            result = await source.collect(instruments, date.fromisoformat(as_of))
        except Exception as exc:  # noqa: BLE001 - source failures are retried
            await self._retry_items(rows, error=str(exc))
            return
        await self._persist_source_result(source, result, date.fromisoformat(as_of))
        now = datetime.now(UTC)
        for row in rows:
            await self.store.complete_refresh_item(
                str(row["job_id"]),
                str(row["source"]),
                str(row["ticker"]),
                now=now,
            )

    async def _retry_items(self, rows: list[dict[str, object]], *, error: str) -> None:
        now = datetime.now(UTC)
        for row in rows:
            raw_attempts = row.get("attempts")
            attempts = raw_attempts if isinstance(raw_attempts, int) else 1
            if attempts >= self.job_max_attempts:
                await self.store.fail_refresh_item(
                    str(row["job_id"]),
                    str(row["source"]),
                    str(row["ticker"]),
                    error=error,
                    now=now,
                )
                continue
            await self.store.requeue_refresh_item(
                str(row["job_id"]),
                str(row["source"]),
                str(row["ticker"]),
                error=error,
                available_at=now + timedelta(seconds=min(60, 2**attempts)),
                now=now,
            )

    async def _fail_items(self, rows: list[dict[str, object]], *, error: str) -> None:
        now = datetime.now(UTC)
        for row in rows:
            await self.store.fail_refresh_item(
                str(row["job_id"]),
                str(row["source"]),
                str(row["ticker"]),
                error=error,
                now=now,
            )

    async def _publish_finished_job(self, job_id: str) -> None:
        if await self.store.pending_refresh_item_count(job_id):
            return
        tickers = await self.store.job_tickers(job_id)
        resolved = resolve_income_events(await self.store.observations(tickers))
        await self.store.publish(resolved, scope_tickers=tickers)
        job = await self.store.refresh_job(job_id)
        failed = 0
        if job is not None:
            raw_failed = job.get("failed")
            failed = raw_failed if isinstance(raw_failed, int) else 0
        await self.store.finish_refresh_job(
            job_id,
            status=REFRESH_PARTIAL if failed else REFRESH_COMPLETED,
            error=None,
            now=datetime.now(UTC),
        )

    async def batch(self, request: IncomeEventBatchRequest) -> IncomeEventBatchResponse:
        return IncomeEventBatchResponse(
            events=await self.store.events(
                request.tickers,
                from_date=request.from_date,
                to_date=request.to_date,
                include_tentative=request.include_tentative,
            ),
            cursor=await self.store.cursor(),
        )

    async def changes(self, cursor: int, limit: int) -> IncomeEventChangesResponse:
        events, next_cursor, has_more = await self.store.changes(cursor, limit=limit)
        return IncomeEventChangesResponse(events=events, cursor=next_cursor, has_more=has_more)


def _coverage_item(coverage: IncomeSourceCoverage) -> IncomeEventCoverageItem:
    return IncomeEventCoverageItem(
        source=coverage.source,
        ticker=coverage.ticker,
        status=coverage.status,
        complete=coverage.complete,
        observed_at=coverage.observed_at,
        detail=coverage.detail,
    )


def _unique_instruments(
    instruments: list[IncomeInstrumentRequest],
) -> list[IncomeInstrumentRequest]:
    unique: dict[tuple[str, str | None], IncomeInstrumentRequest] = {}
    for instrument in instruments:
        unique.setdefault((instrument.ticker, instrument.isin), instrument)
    return list(unique.values())


__all__ = ["IncomeEventService"]
