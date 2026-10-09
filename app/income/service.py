from __future__ import annotations

import asyncio
import logging
import re
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
    IncomeEventBackfillRequest,
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
        job_page_timeout_seconds: float | None = None,
    ) -> None:
        self.store = store
        self.sources = sources
        # ``getattr`` keeps lightweight embedding and test doubles compatible
        # while the protocol marks the official publications.
        self.official_sources = [source for source in sources if getattr(source, "official", False)]
        self.snapshot_overlap_days = snapshot_overlap_days
        self.refresh_ttl_seconds = refresh_ttl_seconds
        self.job_batch_size = max(job_batch_size, 1)
        self.job_lease_seconds = max(job_lease_seconds, 1)
        self.job_max_attempts = max(job_max_attempts, 1)
        self.worker_poll_seconds = max(worker_poll_seconds, 0.01)
        self.job_page_timeout_seconds = (
            max(job_page_timeout_seconds, 0.1) if job_page_timeout_seconds is not None else None
        )
        self._inflight: dict[
            tuple[tuple[tuple[str, str | None], ...], date],
            asyncio.Task[IncomeEventRefreshResponse],
        ] = {}
        self._lock = asyncio.Lock()
        self._publish_lock = asyncio.Lock()
        self._closed = False
        self._worker_task: asyncio.Task[None] | None = None
        self._detached_source_tasks: set[asyncio.Task[IncomeSourceResult]] = set()
        self._detached_source_names: dict[asyncio.Task[IncomeSourceResult], str] = {}
        self._busy_sources: set[str] = set()
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
        if self._detached_source_tasks:
            await asyncio.gather(
                *(asyncio.shield(task) for task in tuple(self._detached_source_tasks)),
                return_exceptions=True,
            )
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
            *(
                self._collect_and_persist_source(source, instruments, as_of)
                for source in self.sources
            ),
            return_exceptions=True,
        )
        observations = 0
        failed_sources: list[str] = []
        successful_sources = 0
        published_early = 0
        for source, result in zip(self.sources, results, strict=True):
            if isinstance(result, BaseException):
                failed_sources.append(source.name)
                continue
            successful_sources += 1
            source_observations, complete, source_published = result
            observations += source_observations
            published_early += source_published
            if not complete:
                failed_sources.append(source.name)
        # A store/provider failure for every source leaves the existing
        # canonical snapshot untouched.  Avoid a second read while the failed
        # producer may still be unwinding its connection transaction.
        if successful_sources == 0 and self.sources:
            return IncomeEventRefreshResponse(
                requested=len(instruments),
                observations=observations,
                published=0,
                failed_sources=sorted(set(failed_sources)),
                cursor=0,
            )
        tickers = [item.ticker for item in instruments]
        resolved = resolve_income_events(await self.store.observations(tickers))
        published = await self.store.publish(resolved, scope_tickers=tickers)
        return IncomeEventRefreshResponse(
            requested=len(instruments),
            observations=observations,
            published=published_early + published,
            failed_sources=sorted(set(failed_sources)),
            cursor=await self.store.cursor(),
        )

    async def _collect_and_persist_source(
        self,
        source: IncomeSource,
        instruments: list[IncomeInstrumentRequest],
        as_of: date,
    ) -> tuple[int, bool, int]:
        """Collect and persist one source without waiting on other sources."""

        result = await self._collect_source(source, instruments, as_of)
        complete = await self._persist_source_result(source, result, as_of)
        complete_tickers = sorted(
            {item.ticker.upper() for item in result.coverage if item.complete}
        )
        published = await self._publish_tickers(complete_tickers)
        return len(result.observations), complete, published

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
        return await self._submit_job(
            self.sources,
            _unique_instruments(request.instruments),
            request.as_of or date.today(),
        )

    async def backfill(
        self,
        request: IncomeEventBackfillRequest,
    ) -> IncomeEventAsyncRefreshResponse:
        """Queue one durable official backfill for a large instrument list.

        Only official B3/CVM publications are collected, so a catalog backfill
        never scrapes the complementary HTML providers. The source keeps the
        CVM open-data index and the parsed documents cached while the worker
        drains the job pages, so a listing shared by many tickers is read once.
        """
        return await self._submit_job(
            self.official_sources,
            _unique_instruments(request.instruments),
            request.as_of or date.today(),
        )

    async def _submit_job(
        self,
        sources: list[IncomeSource],
        instruments: list[IncomeInstrumentRequest],
        as_of: date,
    ) -> IncomeEventAsyncRefreshResponse:
        items: list[tuple[str, IncomeInstrumentRequest]] = []
        for source in sources:
            items.extend(
                (source.name, item) for item in await self._stale_instruments(source, instruments)
            )
        job_id = token_hex(12)
        now = datetime.now(UTC)
        queued = await self.store.create_refresh_job(
            job_id,
            items,
            requested=len(instruments),
            as_of=as_of,
            now=now,
            requested_instruments=instruments,
        )
        pending = await self.store.pending_refresh_item_count(job_id)
        if queued or pending:
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
            status="queued" if (queued or pending) else REFRESH_COMPLETED,
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
            await self._finalize_ready_jobs()
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
            await self._try_publish_finished_job(job_id)
        await self._finalize_ready_jobs()
        return len(items)

    async def _finalize_ready_jobs(self) -> None:
        """Retry publication for jobs whose item work finished previously."""

        job_ids = await self.store.refresh_jobs_ready_for_publication(limit=self.job_batch_size)
        for job_id in job_ids:
            await self._try_publish_finished_job(job_id)

    async def _try_publish_finished_job(self, job_id: str) -> None:
        try:
            await self._publish_finished_job(job_id)
        except Exception:  # noqa: BLE001 - publication retries on the next drain
            _LOGGER.exception("income refresh publication failed", extra={"job_id": job_id})

    async def _process_item_page(
        self,
        source_name: str,
        as_of: str,
        rows: list[dict[str, object]],
        known_sources: dict[str, IncomeSource],
    ) -> None:
        source = known_sources.get(source_name)
        if source is None:
            await self._fail_items(
                rows,
                error="unknown source",
                claim_tokens={
                    (str(row["job_id"]), str(row["source"]), str(row["ticker"])): _optional_text(
                        row.get("claim_token")
                    )
                    for row in rows
                },
            )
            return
        if source_name in self._busy_sources:
            await self._retry_items(
                rows,
                error="source page still draining",
                claim_tokens={
                    (str(row["job_id"]), str(row["source"]), str(row["ticker"])): _optional_text(
                        row.get("claim_token")
                    )
                    for row in rows
                },
            )
            return
        instruments = [
            IncomeInstrumentRequest(
                ticker=str(row["ticker"]),
                isin=_optional_text(row.get("isin")),
                name=_optional_text(row.get("name")),
            )
            for row in rows
        ]
        claim_tokens = {
            (str(row["job_id"]), str(row["source"]), str(row["ticker"])): _optional_text(
                row.get("claim_token")
            )
            for row in rows
        }
        heartbeat_stop = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat_items(rows, claim_tokens, heartbeat_stop))
        try:
            try:
                result = await self._collect_page(source, instruments, date.fromisoformat(as_of))
            except Exception as exc:  # noqa: BLE001 - source failures are retried
                await self._retry_items(
                    rows,
                    error=_source_error(exc),
                    claim_tokens=claim_tokens,
                )
                return
            owned_rows: list[dict[str, object]] = []
            for row in rows:
                key = (str(row["job_id"]), str(row["source"]), str(row["ticker"]))
                if await self.store.refresh_item_owned(
                    *key,
                    claim_token=claim_tokens.get(key),
                ):
                    owned_rows.append(row)
            if not owned_rows:
                return
            owned_tickers = {str(row["ticker"]).upper() for row in owned_rows}
            result = IncomeSourceResult(
                [item for item in result.observations if item.ticker.upper() in owned_tickers],
                [item for item in result.coverage if item.ticker.upper() in owned_tickers],
            )
            try:
                await self._persist_source_result(source, result, date.fromisoformat(as_of))
            except Exception as exc:  # noqa: BLE001 - persistence failures are retried
                await self._retry_items(
                    owned_rows,
                    error=_source_error(exc),
                    claim_tokens=claim_tokens,
                )
                return
            coverage = {
                item.ticker.upper(): item for item in result.coverage if item.source == source.name
            }
            complete_rows: list[dict[str, object]] = []
            incomplete_rows: list[dict[str, object]] = []
            for row in owned_rows:
                item_coverage = coverage.get(str(row["ticker"]).upper())
                if item_coverage is not None and item_coverage.complete:
                    complete_rows.append(row)
                else:
                    incomplete_rows.append(row)
            now = datetime.now(UTC)
            published_tickers: set[str] = set()
            for row in complete_rows:
                key = (str(row["job_id"]), str(row["source"]), str(row["ticker"]))
                completed = await self.store.complete_refresh_item(
                    *key,
                    now=now,
                    claim_token=claim_tokens.get(key),
                )
                if completed:
                    published_tickers.add(str(row["ticker"]).upper())
            if incomplete_rows:
                await self._retry_items(
                    incomplete_rows,
                    error="source coverage incomplete",
                    claim_tokens=claim_tokens,
                )
            if published_tickers:
                await self._publish_tickers(sorted(published_tickers))
        finally:
            heartbeat_stop.set()
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _collect_page(
        self,
        source: IncomeSource,
        instruments: list[IncomeInstrumentRequest],
        as_of: date,
    ) -> IncomeSourceResult:
        """Bound the worker page while allowing shielded source cleanup to drain."""

        task = asyncio.create_task(source.collect(instruments, as_of))
        if self.job_page_timeout_seconds is None:
            return await task
        try:
            return await asyncio.wait_for(
                asyncio.shield(task),
                timeout=self.job_page_timeout_seconds,
            )
        except TimeoutError:
            self._detached_source_tasks.add(task)
            self._detached_source_names[task] = source.name
            self._busy_sources.add(source.name)
            task.add_done_callback(self._cleanup_detached_source_task)
            raise

    def _cleanup_detached_source_task(
        self,
        task: asyncio.Future[IncomeSourceResult],
    ) -> None:
        if isinstance(task, asyncio.Task):
            self._detached_source_tasks.discard(task)
            source_name = self._detached_source_names.pop(task, None)
            if source_name is not None and source_name not in self._detached_source_names.values():
                self._busy_sources.discard(source_name)
        if not task.cancelled():
            task.exception()

    async def _retry_items(
        self,
        rows: list[dict[str, object]],
        *,
        error: str,
        claim_tokens: dict[tuple[str, str, str], str | None] | None = None,
    ) -> None:
        now = datetime.now(UTC)
        for row in rows:
            raw_attempts = row.get("attempts")
            attempts = raw_attempts if isinstance(raw_attempts, int) else 1
            key = (str(row["job_id"]), str(row["source"]), str(row["ticker"]))
            claim_token = claim_tokens.get(key) if claim_tokens is not None else None
            if attempts >= self.job_max_attempts:
                await self.store.fail_refresh_item(
                    *key,
                    error=error,
                    now=now,
                    claim_token=claim_token,
                )
                continue
            await self.store.requeue_refresh_item(
                *key,
                error=error,
                available_at=now + timedelta(seconds=min(60, 2**attempts)),
                now=now,
                claim_token=claim_token,
            )

    async def _fail_items(
        self,
        rows: list[dict[str, object]],
        *,
        error: str,
        claim_tokens: dict[tuple[str, str, str], str | None] | None = None,
    ) -> None:
        now = datetime.now(UTC)
        for row in rows:
            key = (str(row["job_id"]), str(row["source"]), str(row["ticker"]))
            await self.store.fail_refresh_item(
                *key,
                error=error,
                now=now,
                claim_token=claim_tokens.get(key) if claim_tokens is not None else None,
            )

    async def _heartbeat_items(
        self,
        rows: list[dict[str, object]],
        claim_tokens: dict[tuple[str, str, str], str | None],
        stop: asyncio.Event,
    ) -> None:
        interval = max(min(self.job_lease_seconds / 3, 30), 0.1)
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            now = datetime.now(UTC)
            await asyncio.gather(
                *(
                    self.store.renew_refresh_item(
                        str(row["job_id"]),
                        str(row["source"]),
                        str(row["ticker"]),
                        claim_token=token,
                        lease_seconds=self.job_lease_seconds,
                        now=now,
                    )
                    for row in rows
                    if (
                        token := claim_tokens.get(
                            (str(row["job_id"]), str(row["source"]), str(row["ticker"]))
                        )
                    )
                ),
                return_exceptions=True,
            )

    async def _publish_finished_job(self, job_id: str, *, _seen: set[str] | None = None) -> None:
        seen = _seen if _seen is not None else set()
        if job_id in seen:
            return
        seen.add(job_id)
        if await self.store.pending_refresh_item_count(job_id):
            return
        tickers = await self.store.job_tickers(job_id)
        await self._publish_tickers(tickers)
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
        dependents = await self.store.refresh_job_dependents(job_id)
        for dependent in dependents:
            await self._publish_finished_job(dependent, _seen=seen)

    async def _publish_tickers(self, tickers: list[str]) -> int:
        if not tickers:
            return 0
        async with self._publish_lock:
            resolved = resolve_income_events(await self.store.observations(tickers))
            return await self.store.publish(resolved, scope_tickers=tickers)

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


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


_SAFE_SOURCE_ERROR = re.compile(r"^[A-Za-z0-9 _.:/\\-]{1,120}$")


def _source_error(error: BaseException) -> str:
    """Return a bounded provider error without copying credentials to jobs."""

    detail = str(error).strip()
    lowered = detail.lower()
    if (
        not detail
        or not _SAFE_SOURCE_ERROR.fullmatch(detail)
        or any(marker in lowered for marker in ("token", "secret", "password", "bearer"))
    ):
        return f"{type(error).__name__}"
    return detail[:200]


def _unique_instruments(
    instruments: list[IncomeInstrumentRequest],
) -> list[IncomeInstrumentRequest]:
    unique: dict[tuple[str, str | None], IncomeInstrumentRequest] = {}
    for instrument in instruments:
        unique.setdefault((instrument.ticker, instrument.isin), instrument)
    return list(unique.values())


__all__ = ["IncomeEventService"]
