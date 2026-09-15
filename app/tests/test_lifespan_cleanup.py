import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI

import app.main as main


class _Failure(RuntimeError):
    pass


class _Resource:
    def __init__(
        self,
        name: str,
        events: list[str],
        *,
        fail_stage: str | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self.name = name
        self.events = events
        self.fail_stage = fail_stage
        self.close_error = close_error

    async def startup(self) -> None:
        self.events.append(f"{self.name}.startup")
        if self.fail_stage == "startup":
            raise _Failure(f"{self.name} startup failed")

    async def cleanup_before(self, _before: object) -> None:
        self.events.append(f"{self.name}.cleanup_before")
        if self.fail_stage == "cleanup_before":
            raise _Failure(f"{self.name} cleanup failed")

    async def close(self) -> None:
        self.events.append(f"{self.name}.close")
        if self.close_error is not None:
            raise self.close_error

    async def shutdown(self) -> None:
        self.events.append(f"{self.name}.shutdown")
        if self.close_error is not None:
            raise self.close_error


class _InstrumentDataResource(_Resource):
    def __init__(self, events: list[str], **kwargs: Any) -> None:
        super().__init__("instrument", events, **kwargs)
        self.warm_started = asyncio.Event()

    async def warm_directory(self) -> None:
        self.events.append("instrument.warm_directory")
        self.warm_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.events.append("instrument.warm_cancelled")
            raise


class _Dependency:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass


@pytest.fixture
def lifespan_dependencies(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    events: list[str] = []
    resources: dict[str, _Resource] = {}
    config: dict[str, Any] = {
        "failure_name": None,
        "failure_stage": None,
        "close_name": None,
    }

    settings = SimpleNamespace(
        sqlite_cache_enabled=False,
        sqlite_cache_path=Path(".cache/test-lifespan.sqlite3"),
        resolved_assessment_sqlite_path=Path(".cache/test-assessments.sqlite3"),
        database_url=None,
        assessment_lease_seconds=60,
        assessment_max_attempts=3,
        assessment_period_history_days=30,
        assessment_retry_backoff_seconds=1.0,
        income_snapshot_overlap_days=30,
        income_refresh_ttl_seconds=60,
    )

    class Cache(_Resource):
        def __init__(self, **_kwargs: Any) -> None:
            close_error = (
                _Failure("cache close failed") if config["close_name"] == "cache" else None
            )
            super().__init__("cache", events, close_error=close_error)
            resources["cache"] = self

    class Client(_Resource):
        def __init__(self, _settings: object) -> None:
            close_error = (
                _Failure("client shutdown failed") if config["close_name"] == "client" else None
            )
            super().__init__("client", events, close_error=close_error)
            resources["client"] = self

    class Income(_Resource):
        def __init__(self, _path: Path) -> None:
            close_error = (
                _Failure("income close failed") if config["close_name"] == "income" else None
            )
            super().__init__("income", events, fail_stage=None, close_error=close_error)
            resources["income"] = self

    class Instrument(_InstrumentDataResource):
        def __init__(self, _settings: object) -> None:
            fail_stage = config["failure_stage"] if config["failure_name"] == "instrument" else None
            close_error = (
                _Failure("instrument close failed")
                if config["close_name"] == "instrument"
                else None
            )
            super().__init__(events, fail_stage=fail_stage, close_error=close_error)
            resources["instrument"] = self

    class FixedIncome(_Resource):
        def __init__(self, _settings: object, _cache: object) -> None:
            super().__init__("fixed_income", events)
            resources["fixed_income"] = self

    class Assessment(_Resource):
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            fail_stage = config["failure_stage"] if config["failure_name"] == "assessment" else None
            close_error = (
                _Failure("assessment close failed")
                if config["close_name"] == "assessment"
                else None
            )
            super().__init__("assessment", events, fail_stage=fail_stage, close_error=close_error)
            resources["assessment"] = self

    class ClientWithFailure(Client):
        async def startup(self) -> None:
            self.events.append("client.startup")
            if config["failure_name"] == "client":
                raise _Failure("client startup failed")

    class CacheWithFailure(Cache):
        async def startup(self) -> None:
            self.events.append("cache.startup")
            if config["failure_name"] == "cache":
                raise _Failure("cache startup failed")

    class IncomeWithFailure(Income):
        async def startup(self) -> None:
            self.events.append("income.startup")
            if config["failure_name"] == "income":
                raise _Failure("income startup failed")

    class AssessmentWithFailure(Assessment):
        async def startup(self) -> None:
            self.events.append("assessment.startup")
            if config["failure_name"] == "assessment":
                raise _Failure("assessment startup failed")

    class AssessmentWithCleanupFailure(Assessment):
        async def cleanup_before(self, _before: object) -> None:
            self.events.append("assessment.cleanup_before")
            if config["failure_name"] == "assessment_cleanup":
                raise _Failure("assessment cleanup failed")

    def make_assessment(*args: Any, **kwargs: Any) -> _Resource:
        if config["failure_name"] == "assessment":
            return AssessmentWithFailure(*args, **kwargs)
        if config["failure_name"] == "assessment_cleanup":
            return AssessmentWithCleanupFailure(*args, **kwargs)
        return Assessment(*args, **kwargs)

    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "CacheStore", CacheWithFailure)
    monkeypatch.setattr(main, "FundamentusClient", ClientWithFailure)
    monkeypatch.setattr(main, "IncomeEventStore", IncomeWithFailure)
    monkeypatch.setattr(main, "InstrumentDataService", Instrument)
    monkeypatch.setattr(main, "FixedIncomeValuationService", FixedIncome)
    monkeypatch.setattr(main, "AssessmentStore", make_assessment)
    for name in (
        "FundamentusScraper",
        "AssetService",
        "StatusInvestIncomeSource",
        "OfficialCompanyIncomeSource",
        "FundosNetIncomeSource",
        "FundamentusIncomeSource",
        "IncomeEventService",
        "OpportunityService",
        "HistoricalQuoteService",
        "FundamentalsService",
        "QualityFactsService",
        "BcbMacroProvider",
        "BcbBankProvider",
        "AssessmentSnapshotService",
    ):
        monkeypatch.setattr(main, name, _Dependency)

    return {"config": config, "events": events, "resources": resources, "settings": settings}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_name", "failure_stage", "expected_closed"),
    [
        ("cache", "startup", ("cache",)),
        ("client", "startup", ("cache", "client")),
        ("income", "startup", ("cache", "client", "income")),
        (
            "assessment",
            "startup",
            ("assessment", "fixed_income", "instrument", "income", "client", "cache"),
        ),
        (
            "assessment_cleanup",
            "cleanup_before",
            ("assessment", "fixed_income", "instrument", "income", "client", "cache"),
        ),
    ],
)
async def test_lifespan_closes_resources_when_startup_fails(
    lifespan_dependencies: dict[str, Any],
    failure_name: str,
    failure_stage: str,
    expected_closed: tuple[str, ...],
) -> None:
    config = lifespan_dependencies["config"]
    config["failure_name"] = failure_name
    config["failure_stage"] = failure_stage
    app = FastAPI()

    with pytest.raises(_Failure, match="failed"):
        async with main.lifespan(app):
            pytest.fail("startup should fail before yielding")

    resources = lifespan_dependencies["resources"]
    closers = [name for name in expected_closed if name != "client"]
    assert all(resources[name].events.count(f"{name}.close") == 1 for name in closers)
    for name, resource in resources.items():
        if name not in expected_closed:
            assert resource.events.count(f"{name}.close") == 0
            assert resource.events.count(f"{name}.shutdown") == 0
    if "client" in expected_closed:
        assert resources["client"].events.count("client.shutdown") == 1
    if "instrument" in expected_closed:
        warm_task = app.state.instrument_directory_warm_task
        assert warm_task.done()
        assert warm_task.cancelled()
        assert resources["instrument"].events.count("instrument.close") == 1


@pytest.mark.asyncio
async def test_lifespan_preserves_startup_error_when_cleanup_fails(
    lifespan_dependencies: dict[str, Any],
) -> None:
    config = lifespan_dependencies["config"]
    config["failure_name"] = "assessment"
    config["failure_stage"] = "startup"
    config["close_name"] = "cache"
    app = FastAPI()

    with pytest.raises(_Failure, match="assessment startup failed"):
        async with main.lifespan(app):
            pytest.fail("startup should fail before yielding")

    events = lifespan_dependencies["events"]
    assert events.count("cache.close") == 1
    assert events.count("client.shutdown") == 1
    for name in ("assessment", "fixed_income", "instrument", "income"):
        assert events.count(f"{name}.close") == 1


@pytest.mark.asyncio
async def test_lifespan_normal_shutdown_cancels_warm_task_once(
    lifespan_dependencies: dict[str, Any],
) -> None:
    app = FastAPI()

    async with main.lifespan(app):
        warm_started = lifespan_dependencies["resources"]["instrument"].warm_started
        await asyncio.wait_for(warm_started.wait(), timeout=1)

    events = lifespan_dependencies["events"]
    assert events.count("instrument.close") == 1
    assert events.count("instrument.warm_cancelled") == 1
    warm_task = app.state.instrument_directory_warm_task
    assert warm_task.done()
    assert warm_task.cancelled()
