from __future__ import annotations

import asyncio
from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.metrics import MetricsRegistry, metrics, record_income_phase, set_income_source_busy
from app.income.service import IncomeEventService
from app.income.sources import IncomeSourceResult
from app.income.store import IncomeEventStore
from app.models.income_events import (
    IncomeEventObservation,
    IncomeEventRefreshRequest,
    IncomeInstrumentRequest,
    IncomeSourceCoverage,
)


@pytest.fixture(autouse=True)
def _reset_metrics() -> Iterator[None]:
    metrics.reset()
    yield
    metrics.reset()


def _observation(source: str, ticker: str) -> IncomeEventObservation:
    return IncomeEventObservation(
        source=source,
        lineage=f"lineage:{source}",
        source_event_id=f"{source}:{ticker}:1",
        ticker=ticker,
        event_type="Dividendo",
        ex_date=date(2026, 9, 1),
        payment_date=date(2026, 9, 11),
        unit_price=Decimal("0.50"),
        authority=100,
        payload_hash=f"hash:{source}:{ticker}",
    )


class _MetricSource:
    snapshot_sources: tuple[str, ...]
    official = False

    def __init__(
        self,
        name: str,
        *,
        fail: bool = False,
        gated: bool = False,
    ) -> None:
        self.name = name
        self.snapshot_sources = (name,)
        self.fail = fail
        self.calls = 0
        self.started = asyncio.Event() if gated else None
        self.release = asyncio.Event() if gated else None
        self.finished = asyncio.Event() if gated else None

    async def collect(
        self,
        instruments: Sequence[IncomeInstrumentRequest],
        as_of: date,
    ) -> IncomeSourceResult:
        del as_of
        self.calls += 1
        if self.started is not None:
            self.started.set()
        try:
            if self.release is not None:
                await self.release.wait()
            if self.fail:
                raise RuntimeError("offline")
            ticker = instruments[0].ticker
            return IncomeSourceResult(
                [_observation(self.name, ticker)],
                [
                    IncomeSourceCoverage(
                        source=self.name,
                        ticker=ticker,
                        status="complete",
                        complete=True,
                    )
                ],
            )
        finally:
            if self.finished is not None:
                self.finished.set()


class _LostClaimStore(IncomeEventStore):
    async def refresh_item_owned(
        self,
        job_id: str,
        source: str,
        ticker: str,
        *,
        claim_token: str | None,
    ) -> bool:
        del job_id, source, ticker, claim_token
        return False


def _operation_line(rendered: str, *, source: str, phase: str, outcome: str) -> str:
    for line in rendered.splitlines():
        if not line.startswith("fundamentus_api_income_producer_operations_total{"):
            continue
        if (
            f'source="{source}"' in line
            and f'phase="{phase}"' in line
            and f'outcome="{outcome}"' in line
        ):
            return line
    raise AssertionError(f"missing operation sample: {source=} {phase=} {outcome=}")


@pytest.mark.asyncio
async def test_successful_producer_records_bounded_phase_metrics(tmp_path: Path) -> None:
    store = IncomeEventStore(tmp_path / "income.sqlite3")
    await store.startup()
    service = IncomeEventService(store, [_MetricSource("fundamentus")])
    try:
        accepted = await service.refresh_async(
            IncomeEventRefreshRequest(
                instruments=[IncomeInstrumentRequest(ticker="BBAS3")],
                as_of=date(2026, 9, 18),
                mode="async",
            )
        )
        assert await service.process_pending_once() == 1
        state = await service.refresh_job(accepted.job_id)
        assert state is not None and state["status"] == "completed"
        assert len(await store.events(["BBAS3"])) == 1

        rendered = metrics.render_prometheus()
        for phase in ("collect", "persist", "publish"):
            assert _operation_line(
                rendered,
                source="fundamentus",
                phase=phase,
                outcome="success",
            ).endswith(" 1")
        assert _operation_line(
            rendered,
            source="other",
            phase="worker",
            outcome="success",
        ).endswith(" 1")
        assert "fundamentus_api_income_producer_duration_seconds_count{" in rendered
    finally:
        await service.close()
        await store.close()


@pytest.mark.asyncio
async def test_failed_source_records_failure_without_changing_job_result(tmp_path: Path) -> None:
    store = IncomeEventStore(tmp_path / "income.sqlite3")
    await store.startup()
    service = IncomeEventService(
        store,
        [_MetricSource("status_invest", fail=True)],
        job_max_attempts=1,
    )
    try:
        accepted = await service.refresh_async(
            IncomeEventRefreshRequest(
                instruments=[IncomeInstrumentRequest(ticker="BBAS3")], mode="async"
            )
        )
        assert await service.process_pending_once() == 1
        state = await service.refresh_job(accepted.job_id)
        assert state is not None and state["status"] == "partial"
        assert _operation_line(
            metrics.render_prometheus(),
            source="status_invest",
            phase="collect",
            outcome="failed",
        ).endswith(" 1")
    finally:
        await service.close()
        await store.close()


@pytest.mark.asyncio
async def test_timeout_and_detached_drain_have_separate_outcomes(tmp_path: Path) -> None:
    store = IncomeEventStore(tmp_path / "income.sqlite3")
    await store.startup()
    source = _MetricSource("official_companies", gated=True)
    service = IncomeEventService(
        store,
        [source],
        job_page_timeout_seconds=0.1,
        job_max_attempts=1,
    )
    try:
        await service.refresh_async(
            IncomeEventRefreshRequest(
                instruments=[IncomeInstrumentRequest(ticker="BBAS3")], mode="async"
            )
        )
        assert await service.process_pending_once() == 1
        assert source.started is not None
        await source.started.wait()
        assert _operation_line(
            metrics.render_prometheus(),
            source="official_companies",
            phase="collect",
            outcome="timeout",
        ).endswith(" 1")
        assert (
            'fundamentus_api_income_source_busy{source="official_companies"} 1'
            in metrics.render_prometheus()
        )

        assert source.release is not None and source.finished is not None
        source.release.set()
        await asyncio.wait_for(source.finished.wait(), timeout=1)
        for _ in range(100):
            if not service._detached_source_tasks:
                break
            await asyncio.sleep(0.01)
        assert not service._detached_source_tasks
        assert _operation_line(
            metrics.render_prometheus(),
            source="official_companies",
            phase="collect",
            outcome="drained",
        ).endswith(" 1")
        assert (
            'fundamentus_api_income_source_busy{source="official_companies"} 0'
            in metrics.render_prometheus()
        )
    finally:
        if source.release is not None:
            source.release.set()
        await service.close()
        await store.close()


@pytest.mark.asyncio
async def test_cancelled_collection_is_observed_and_cancellation_propagates(
    tmp_path: Path,
) -> None:
    store = IncomeEventStore(tmp_path / "income.sqlite3")
    await store.startup()
    source = _MetricSource("fundamentus", gated=True)
    service = IncomeEventService(store, [source])
    rows: list[dict[str, object]] = [
        {
            "job_id": "cancel-job",
            "source": source.name,
            "ticker": "BBAS3",
            "attempts": 1,
            "claim_token": None,
        }
    ]
    task = asyncio.create_task(
        service._process_item_page(
            source.name,
            "2026-09-18",
            rows,
            {source.name: source},
        )
    )
    try:
        assert source.started is not None and source.finished is not None
        await source.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(source.finished.wait(), timeout=1)
        assert _operation_line(
            metrics.render_prometheus(),
            source="fundamentus",
            phase="collect",
            outcome="cancelled",
        ).endswith(" 1")
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await service.close()
        await store.close()


@pytest.mark.asyncio
async def test_lost_claim_and_busy_deferral_are_low_cardinality_outcomes(
    tmp_path: Path,
) -> None:
    lost_store = _LostClaimStore(tmp_path / "lost.sqlite3")
    await lost_store.startup()
    lost_source = _MetricSource("fundamentus")
    lost_service = IncomeEventService(lost_store, [lost_source])
    rows: list[dict[str, object]] = [
        {
            "job_id": "lost-job",
            "source": "fundamentus",
            "ticker": "BBAS3",
            "attempts": 1,
            "claim_token": None,
        }
    ]
    try:
        await lost_service._process_item_page(
            "fundamentus", "2026-09-18", rows, {"fundamentus": lost_source}
        )
        assert _operation_line(
            metrics.render_prometheus(),
            source="fundamentus",
            phase="worker",
            outcome="lost_claim",
        ).endswith(" 1")
    finally:
        await lost_service.close()
        await lost_store.close()

    busy_store = IncomeEventStore(tmp_path / "busy.sqlite3")
    await busy_store.startup()
    busy_source = _MetricSource("official_companies")
    busy_service = IncomeEventService(busy_store, [busy_source])
    busy_service._busy_sources.add(busy_source.name)
    try:
        await busy_service._process_item_page(
            busy_source.name,
            "2026-09-18",
            rows,
            {busy_source.name: busy_source},
        )
        assert _operation_line(
            metrics.render_prometheus(),
            source="official_companies",
            phase="worker",
            outcome="deferred",
        ).endswith(" 1")
        assert busy_source.calls == 0
    finally:
        await busy_service.close()
        await busy_store.close()


@pytest.mark.asyncio
async def test_unknown_source_is_mapped_to_other_without_identifier_label(tmp_path: Path) -> None:
    store = IncomeEventStore(tmp_path / "income.sqlite3")
    await store.startup()
    service = IncomeEventService(store, [_MetricSource("fundamentus")])
    now = datetime(2026, 9, 18, tzinfo=UTC)
    try:
        await store.create_refresh_job(
            "ghost-job",
            [("mystery-provider", "BBAS3")],
            requested=1,
            as_of=now.date(),
            now=now,
        )
        assert await service.process_pending_once() == 1
        state = await service.refresh_job("ghost-job")
        assert state is not None and state["status"] == "partial"
        rendered = metrics.render_prometheus()
        assert _operation_line(
            rendered,
            source="other",
            phase="worker",
            outcome="failed",
        ).endswith(" 1")
        assert "mystery-provider" not in rendered
    finally:
        await service.close()
        await store.close()


def test_registry_keeps_legacy_counters_and_resets_bounded_histograms() -> None:
    registry = MetricsRegistry()
    registry.inc("legacy_counter", amount=2)
    registry.inc("labeled_counter", labels={"source": "fundamentus"})
    registry.set_gauge("busy", 1, labels={"source": "fundamentus"})
    registry.observe("latency_seconds", 0.2, labels={"source": "fundamentus"})
    registry.observe("plain_latency_seconds", 0.1)
    registry.observe("latency_seconds", -1, labels={"source": "fundamentus"})
    registry.observe("latency_seconds", float("inf"), labels={"source": "fundamentus"})
    registry.set_gauge("invalid", float("inf"), labels={"source": "fundamentus"})
    rendered = registry.render_prometheus()
    assert "fundamentus_api_legacy_counter_total 2" in rendered
    assert 'fundamentus_api_labeled_counter_total{source="fundamentus"} 1' in rendered
    assert 'fundamentus_api_busy{source="fundamentus"} 1' in rendered
    assert 'fundamentus_api_latency_seconds_count{source="fundamentus"} 1' in rendered
    assert "fundamentus_api_plain_latency_seconds_count 1" in rendered
    registry.reset()
    assert "legacy_counter" not in registry.render_prometheus()


def test_income_metric_labels_fallback_to_fixed_values() -> None:
    record_income_phase("untrusted-source", "unknown-phase", "untrusted-outcome", 0.1)
    rendered = metrics.render_prometheus()
    assert 'source="other"' in rendered
    assert 'phase="other"' in rendered
    assert 'outcome="other"' in rendered
    assert "untrusted-source" not in rendered
    assert "unknown-phase" not in rendered
    assert "untrusted-outcome" not in rendered


def test_telemetry_failures_never_escape_producer_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("metrics unavailable")

    monkeypatch.setattr(metrics, "inc", fail)
    record_income_phase("fundamentus", "collect", "success", 0.1)
    monkeypatch.setattr(metrics, "set_gauge", fail)
    set_income_source_busy("fundamentus", True)
