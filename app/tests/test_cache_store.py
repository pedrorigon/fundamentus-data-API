from __future__ import annotations

import asyncio
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.cache import CacheStore
from app.cache.sqlite import _rollback, _transaction_body
from app.config import Settings
from app.income.store import IncomeEventStore
from app.models.income_events import IncomeEventObservation


def _row_count(path: Path, table: str) -> int:
    with closing(sqlite3.connect(path)) as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _observation() -> IncomeEventObservation:
    return IncomeEventObservation(
        source="test",
        lineage="test",
        source_event_id="test-event",
        ticker="BBAS3",
        event_type="Dividendo",
        authority=50,
        payload_hash="test-hash",
    )


class _RollbackDb:
    def __init__(self, *, fail: bool = False) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.fail = fail

    async def rollback(self) -> None:
        self.started.set()
        if self.fail:
            raise RuntimeError("rollback failed")
        await self.release.wait()


@pytest.mark.asyncio
async def test_rollback_completes_when_the_calling_task_is_cancelled() -> None:
    database = _RollbackDb()
    task = asyncio.create_task(_rollback(database))  # type: ignore[arg-type]
    await database.started.wait()
    task.cancel()
    database.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    with pytest.raises(RuntimeError, match="rollback failed"):
        await _rollback(_RollbackDb(fail=True))  # type: ignore[arg-type]

    failing = _RollbackDb(fail=True)
    task = asyncio.create_task(_rollback(failing))  # type: ignore[arg-type]
    await failing.started.wait()
    task.cancel()
    with pytest.raises(RuntimeError, match="rollback failed"):
        await task


@pytest.mark.asyncio
async def test_transaction_preserves_primary_error_when_rollback_fails() -> None:
    class _TransactionDb:
        async def execute(self, sql: str) -> None:
            assert sql == "BEGIN IMMEDIATE"

        async def commit(self) -> None:
            raise AssertionError("commit should not run")

        async def rollback(self) -> None:
            raise RuntimeError("rollback failed")

    with pytest.raises(ValueError, match="body failed") as caught:
        async with _transaction_body(_TransactionDb()):  # type: ignore[arg-type]
            raise ValueError("body failed")
    assert "SQLite rollback failed" in " ".join(caught.value.__notes__)


@pytest.mark.asyncio
async def test_expired_reads_are_side_effect_free_and_stale_reads_keep_expiry(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cache.sqlite3"
    cache = CacheStore(sqlite_enabled=True, sqlite_path=path)
    await cache.startup()
    await cache.startup()
    await cache.set("expired", {"value": 1}, ttl_seconds=-1, memory=False)

    value, hit = await cache.get("expired", memory=False)
    assert value is None
    assert hit is False
    assert _row_count(path, "cache_entries") == 1

    stale = await cache.get_stale("expired", memory=False)
    assert stale is not None
    assert stale.value == {"value": 1}
    assert stale.expires_at < time.time()

    assert await cache.cleanup_expired(limit=1) == 1
    assert _row_count(path, "cache_entries") == 0
    await cache.close()


@pytest.mark.asyncio
async def test_cache_startup_cleans_expired_rows_in_a_bounded_batch_and_indexes_expiry(
    tmp_path: Path,
) -> None:
    path = tmp_path / "startup-cleanup.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "CREATE TABLE cache_entries (cache_key TEXT PRIMARY KEY, "
            "expires_at REAL NOT NULL, payload TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO cache_entries(cache_key, expires_at, payload) VALUES (?, ?, ?)",
            [(f"expired-{index}", 0, "null") for index in range(105)],
        )
        connection.execute("INSERT INTO cache_entries VALUES ('fresh', 4102444800, 'null')")
        connection.commit()

    cache = CacheStore(sqlite_enabled=True, sqlite_path=path)
    await cache.startup()

    assert _row_count(path, "cache_entries") == 6
    with closing(sqlite3.connect(path)) as connection:
        indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(cache_entries)").fetchall()
        }
    assert "ix_cache_entries_expires_at" in indexes
    await cache.close()


@pytest.mark.asyncio
async def test_stale_memory_entry_does_not_hide_a_newer_durable_write(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite3"
    original = CacheStore(sqlite_enabled=True, sqlite_path=path)
    newer = CacheStore(sqlite_enabled=True, sqlite_path=path)
    await original.startup()
    await newer.startup()
    await original.set("quote:BBAS3", {"price": 10}, ttl_seconds=-1)
    await newer.set("quote:BBAS3", {"price": 11}, ttl_seconds=60)

    stale = await original.get_stale("quote:BBAS3")

    assert stale is not None
    assert stale.value == {"price": 11}
    assert stale.is_fresh()
    await newer.close()
    await original.close()


@pytest.mark.asyncio
async def test_concurrent_cache_writes_keep_memory_and_disk_in_same_commit_order(
    tmp_path: Path,
) -> None:
    cache = CacheStore(sqlite_enabled=True, sqlite_path=tmp_path / "ordered.sqlite3")
    await cache.startup()

    await asyncio.gather(
        *(cache.set("quote:BBAS3", {"price": index}, ttl_seconds=60) for index in range(64))
    )

    memory_value, memory_hit = await cache.get("quote:BBAS3")
    durable_value, durable_hit = await cache.get("quote:BBAS3", memory=False)
    assert memory_hit and durable_hit
    assert memory_value == durable_value
    await cache.close()


@pytest.mark.asyncio
async def test_memory_cache_is_bounded_and_fresh_reads_promote_lru(tmp_path: Path) -> None:
    cache = CacheStore(
        sqlite_enabled=False,
        sqlite_path=tmp_path / "memory.sqlite3",
        memory_cache_max_entries=2,
    )

    await cache.set("a", "a", ttl_seconds=60)
    await cache.set("b", "b", ttl_seconds=60)
    assert list(cache._memory) == ["a", "b"]

    value, hit = await cache.get("a")
    assert (value, hit) == ("a", True)
    assert list(cache._memory) == ["b", "a"]

    await cache.set("c", "c", ttl_seconds=60)
    assert len(cache._memory) <= 2
    assert list(cache._memory) == ["a", "c"]
    assert await cache.get("b") == (None, False)


@pytest.mark.asyncio
async def test_memory_cache_evicts_expired_entries_before_oldest_lru(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_time = 1_000.0
    monkeypatch.setattr("app.cache.store.time.time", lambda: current_time)
    cache = CacheStore(
        sqlite_enabled=False,
        sqlite_path=tmp_path / "memory.sqlite3",
        memory_cache_max_entries=2,
    )

    await cache.set("expired", "expired", ttl_seconds=5)
    await cache.set("old", "old", ttl_seconds=100)
    current_time = 1_010.0
    await cache.set("new", "new", ttl_seconds=100)

    assert len(cache._memory) <= 2
    assert list(cache._memory) == ["old", "new"]
    assert await cache.get("old") == ("old", True)


@pytest.mark.asyncio
async def test_lru_eviction_keeps_durable_value_retrievable(tmp_path: Path) -> None:
    path = tmp_path / "durable.sqlite3"
    cache = CacheStore(
        sqlite_enabled=True,
        sqlite_path=path,
        memory_cache_max_entries=1,
    )
    await cache.startup()

    await cache.set("first", 1, ttl_seconds=60)
    await cache.set("second", 2, ttl_seconds=60)
    assert list(cache._memory) == ["second"]

    value, hit = await cache.get("first")
    assert (value, hit) == (1, True)
    assert list(cache._memory) == ["first"]
    assert await cache.get("second", memory=False) == (2, True)
    await cache.close()


def test_memory_cache_capacity_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="memory_cache_max_entries must be positive"):
        CacheStore(
            sqlite_enabled=False,
            sqlite_path=tmp_path / "memory.sqlite3",
            memory_cache_max_entries=0,
        )
    with pytest.raises(ValueError):
        Settings(memory_cache_max_entries=0)


@pytest.mark.asyncio
async def test_cache_invalidation_and_bounded_memory_cleanup(tmp_path: Path) -> None:
    memory_cache = CacheStore(sqlite_enabled=False, sqlite_path=tmp_path / "memory.sqlite3")
    await memory_cache.startup()
    await memory_cache.set("details:one", 1, ttl_seconds=-1)
    await memory_cache.set("details:two", 2, ttl_seconds=-1)
    await memory_cache.set("other", 3, ttl_seconds=60)
    with pytest.raises(ValueError, match="limit must be positive"):
        await memory_cache.cleanup_expired(limit=0)
    assert await memory_cache.cleanup_expired(limit=1) == 1
    stale = await memory_cache.get_stale("details:two")
    assert stale is not None
    assert stale.value == 2
    await memory_cache.invalidate(prefix="details:")
    assert await memory_cache.get_entry("details:two") is None
    assert (await memory_cache.get("other"))[0] == 3
    await memory_cache.invalidate()
    assert await memory_cache.get_entry("other") is None

    path = tmp_path / "persistent.sqlite3"
    cache = CacheStore(sqlite_enabled=True, sqlite_path=path)
    await cache.startup()
    await cache.set("details:one", 1, ttl_seconds=60)
    await cache.set("other", 2, ttl_seconds=60)
    await cache.invalidate(prefix="details:")
    assert await cache.get_entry("details:one", memory=False) is None
    assert (await cache.get("other", memory=False))[0] == 2
    await cache.invalidate()
    assert await cache.get_entry("other", memory=False) is None
    await cache.close()


@pytest.mark.asyncio
async def test_cache_serializes_nested_models_before_durable_commit(tmp_path: Path) -> None:
    cache = CacheStore(sqlite_enabled=True, sqlite_path=tmp_path / "nested.sqlite3")
    await cache.startup()
    await cache.set("nested", {"items": [_observation()]}, ttl_seconds=60, memory=False)

    value, hit = await cache.get("nested", memory=False)

    assert hit
    assert isinstance(value, dict)
    assert value["items"][0]["ticker"] == "BBAS3"
    await cache.close()


@pytest.mark.asyncio
async def test_cache_startup_closes_connection_when_schema_setup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "cache.sqlite3"
    cache = CacheStore(sqlite_enabled=True, sqlite_path=path)

    async def fail_setup(_db: object) -> None:
        raise RuntimeError("setup failed")

    monkeypatch.setattr("app.cache.store.configure_sqlite_connection", fail_setup)
    with pytest.raises(RuntimeError, match="setup failed"):
        await cache.startup()
    assert cache._db is None


@pytest.mark.asyncio
async def test_income_startup_closes_connection_when_schema_setup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = IncomeEventStore(tmp_path / "income.sqlite3")

    async def fail_setup(_db: object) -> None:
        raise RuntimeError("setup failed")

    monkeypatch.setattr("app.income.store.configure_sqlite_connection", fail_setup)
    with pytest.raises(RuntimeError, match="setup failed"):
        await store.startup()
    assert store._db is None


@pytest.mark.asyncio
async def test_empty_coverage_read_is_side_effect_free(tmp_path: Path) -> None:
    store = IncomeEventStore(tmp_path / "income.sqlite3")
    await store.startup()
    assert await store.fresh_coverage("source", [], not_before=datetime.now(UTC)) == set()
    await store.close()


@pytest.mark.asyncio
async def test_failed_commit_does_not_publish_memory_or_persistent_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "cache.sqlite3"
    cache = CacheStore(sqlite_enabled=True, sqlite_path=path)
    await cache.startup()
    db = cache._db
    assert db is not None
    original_commit = db.commit

    async def fail_commit() -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(db, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="commit failed"):
        await cache.set("failed", {"value": 1}, ttl_seconds=60)
    monkeypatch.setattr(db, "commit", original_commit)

    assert await cache.get_entry("failed") is None
    assert await cache.get_entry("failed", memory=False) is None
    assert _row_count(path, "cache_entries") == 0
    await cache.close()


@pytest.mark.asyncio
async def test_cancelled_cache_write_rolls_back_and_leaves_memory_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "cache.sqlite3"
    cache = CacheStore(sqlite_enabled=True, sqlite_path=path)
    await cache.startup()
    db = cache._db
    assert db is not None
    original_execute = db.execute
    entered = asyncio.Event()

    async def block_insert(sql: str, *parameters: object) -> object:
        if sql.lstrip().startswith("INSERT INTO cache_entries"):
            entered.set()
            await asyncio.Future()
        return await original_execute(sql, *parameters)

    monkeypatch.setattr(db, "execute", block_insert)
    task = asyncio.create_task(cache.set("cancelled", {"value": 1}, ttl_seconds=60))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    monkeypatch.setattr(db, "execute", original_execute)

    assert await cache.get_entry("cancelled") is None
    assert await cache.get_entry("cancelled", memory=False) is None
    assert _row_count(path, "cache_entries") == 0
    await cache.close()


@pytest.mark.asyncio
async def test_cache_and_income_store_writers_share_path_safely(tmp_path: Path) -> None:
    path = tmp_path / "shared.sqlite3"
    cache = CacheStore(sqlite_enabled=True, sqlite_path=path)
    income = IncomeEventStore(path)
    await cache.startup()
    await income.startup()

    await asyncio.gather(
        *(cache.set(f"key:{index}", index, ttl_seconds=60) for index in range(20)),
        *(income.save_observations([_observation()]) for _ in range(5)),
    )

    values = await asyncio.gather(*(cache.get(f"key:{index}") for index in range(20)))
    assert [value for value, hit in values] == list(range(20))
    assert all(hit for _value, hit in values)
    assert len(await income.observations(["BBAS3"])) == 1

    await income.close()
    await cache.close()


def test_cache_and_income_writers_are_serialized_across_event_loops(tmp_path: Path) -> None:
    """Separate worker loops must share one path lock without losing writes."""

    path = tmp_path / "threaded.sqlite3"

    async def write_from_worker(index: int) -> None:
        cache = CacheStore(sqlite_enabled=True, sqlite_path=path)
        income = IncomeEventStore(path)
        await cache.startup()
        await income.startup()
        try:
            await asyncio.gather(
                cache.set(f"worker:{index}", index, ttl_seconds=600, memory=False),
                income.save_observations(
                    [
                        _observation().model_copy(
                            update={
                                "source": f"worker-{index}",
                                "source_event_id": f"event-{index}",
                            }
                        )
                    ]
                ),
            )
        finally:
            await income.close()
            await cache.close()

    def run_worker(index: int) -> None:
        asyncio.run(write_from_worker(index))

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(run_worker, range(24)))

    async def verify() -> None:
        cache = CacheStore(sqlite_enabled=True, sqlite_path=path)
        income = IncomeEventStore(path)
        await cache.startup()
        await income.startup()
        try:
            values = await asyncio.gather(
                *(cache.get(f"worker:{index}", memory=False) for index in range(24))
            )
            assert [value for value, hit in values] == list(range(24))
            assert all(hit for _value, hit in values)
            observations = await income.observations(["BBAS3"])
            assert len(observations) == 24
        finally:
            await income.close()
            await cache.close()

    asyncio.run(verify())


@pytest.mark.asyncio
async def test_income_write_failure_rolls_back_complete_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = IncomeEventStore(tmp_path / "income.sqlite3")
    await store.startup()
    db = store._db
    assert db is not None
    original_executemany = db.executemany

    async def fail_executemany(*args: object, **kwargs: object) -> None:
        raise RuntimeError("batch failed")

    monkeypatch.setattr(db, "executemany", fail_executemany)
    with pytest.raises(RuntimeError, match="batch failed"):
        await store.save_observations([_observation()])
    monkeypatch.setattr(db, "executemany", original_executemany)

    assert await store.observations(["BBAS3"]) == []
    await store.close()
