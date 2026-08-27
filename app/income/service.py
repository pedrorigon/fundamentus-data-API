from __future__ import annotations

import asyncio
from datetime import date

from app.income.resolver import resolve_income_events
from app.income.sources import IncomeSource
from app.income.store import IncomeEventStore
from app.models.income_events import (
    IncomeEventBatchRequest,
    IncomeEventBatchResponse,
    IncomeEventChangesResponse,
    IncomeEventRefreshRequest,
    IncomeEventRefreshResponse,
    IncomeInstrumentRequest,
)


class IncomeEventService:
    def __init__(self, store: IncomeEventStore, sources: list[IncomeSource]) -> None:
        self.store = store
        self.sources = sources
        self._inflight: dict[
            tuple[tuple[str, str | None], ...], asyncio.Task[IncomeEventRefreshResponse]
        ] = {}
        self._lock = asyncio.Lock()

    async def refresh(self, request: IncomeEventRefreshRequest) -> IncomeEventRefreshResponse:
        instruments = _unique_instruments(request.instruments)
        key = tuple((item.ticker, item.isin) for item in instruments)
        async with self._lock:
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(
                    self._refresh(instruments, request.as_of or date.today())
                )
                self._inflight[key] = task
        try:
            return await task
        finally:
            async with self._lock:
                if self._inflight.get(key) is task:
                    self._inflight.pop(key, None)

    async def _refresh(
        self,
        instruments: list[IncomeInstrumentRequest],
        as_of: date,
    ) -> IncomeEventRefreshResponse:
        results = await asyncio.gather(
            *(source.collect(instruments, as_of) for source in self.sources),
            return_exceptions=True,
        )
        observations = []
        coverage = []
        failed_sources: list[str] = []
        for source, result in zip(self.sources, results, strict=True):
            if isinstance(result, BaseException):
                failed_sources.append(source.name)
                continue
            observations.extend(result.observations)
            coverage.extend(result.coverage)
            complete_tickers = [item.ticker for item in result.coverage if item.complete]
            await self.store.replace_observations(
                result.observations,
                snapshot_sources=source.snapshot_sources,
                complete_tickers=complete_tickers,
            )
            if any(not item.complete for item in result.coverage):
                failed_sources.append(source.name)
        await self.store.save_coverage(coverage)
        tickers = [item.ticker for item in instruments]
        resolved = resolve_income_events(await self.store.observations(tickers))
        published = await self.store.publish(resolved, scope_tickers=tickers)
        return IncomeEventRefreshResponse(
            requested=len(instruments),
            observations=len(observations),
            published=published,
            failed_sources=sorted(set(failed_sources)),
            cursor=await self.store.cursor(),
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


def _unique_instruments(
    instruments: list[IncomeInstrumentRequest],
) -> list[IncomeInstrumentRequest]:
    unique: dict[tuple[str, str | None], IncomeInstrumentRequest] = {}
    for instrument in instruments:
        unique.setdefault((instrument.ticker, instrument.isin), instrument)
    return list(unique.values())


__all__ = ["IncomeEventService"]
