from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite
import orjson
from pydantic import BaseModel

from app.cache.sqlite import (
    configure_sqlite_connection,
    sqlite_path_lock,
    sqlite_transaction,
)

CACHE_MAINTENANCE_BATCH_SIZE = 100


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """A cache value together with the expiry recorded at write time."""

    value: Any
    expires_at: float

    def is_fresh(self, now: float | None = None) -> bool:
        return self.expires_at > (time.time() if now is None else now)


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _to_jsonable(item) for key, item in value.items()}
    return value


class CacheStore:
    def __init__(
        self,
        *,
        sqlite_enabled: bool,
        sqlite_path: Path,
        memory_cache_max_entries: int = 4096,
    ) -> None:
        if memory_cache_max_entries < 1:
            raise ValueError("memory_cache_max_entries must be positive")
        self.sqlite_enabled = sqlite_enabled
        self.sqlite_path = sqlite_path
        self.memory_cache_max_entries = memory_cache_max_entries
        self._memory: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = asyncio.Lock()
        self._db_lock = asyncio.Lock()
        self._db: aiosqlite.Connection | None = None

    async def startup(self) -> None:
        if not self.sqlite_enabled:
            return
        async with self._db_lock:
            if self._db is not None:
                return
            self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
            db = await aiosqlite.connect(self.sqlite_path, timeout=5.0)
            try:
                async with sqlite_path_lock(self.sqlite_path):
                    await configure_sqlite_connection(db)
                    async with sqlite_transaction(db, self.sqlite_path, acquire_lock=False):
                        await db.execute(
                            """
                            CREATE TABLE IF NOT EXISTS cache_entries (
                                cache_key TEXT PRIMARY KEY,
                                expires_at REAL NOT NULL,
                                payload TEXT NOT NULL
                            )
                            """
                        )
                        await db.execute(
                            """
                            CREATE INDEX IF NOT EXISTS ix_cache_entries_expires_at
                                ON cache_entries (expires_at, cache_key)
                            """
                        )
                        # Startup cleanup is deliberately bounded. A stale
                        # cache may contain millions of rows after downtime,
                        # and readiness must never scan or delete the whole
                        # table in one transaction.
                        await db.execute(
                            """
                            DELETE FROM cache_entries
                            WHERE cache_key IN (
                                SELECT cache_key FROM cache_entries
                                WHERE expires_at <= ?
                                ORDER BY expires_at, cache_key
                                LIMIT ?
                            )
                            """,
                            (time.time(), CACHE_MAINTENANCE_BATCH_SIZE),
                        )
                self._db = db
            except BaseException:
                await db.close()
                raise

    async def close(self) -> None:
        async with self._db_lock:
            db = self._db
            if db is not None:
                async with sqlite_path_lock(self.sqlite_path):
                    try:
                        await db.close()
                    finally:
                        self._db = None

    async def get_entry(
        self,
        key: str,
        *,
        memory: bool = True,
        allow_stale: bool = False,
    ) -> CacheEntry | None:
        """Read a cache entry without deleting expired persistent data.

        ``allow_stale`` is explicit so callers that need a last-known value can
        inspect it together with its original expiry.  The regular ``get`` API
        keeps its historical fresh-hit semantics.
        """

        now = time.time()
        if memory:
            stale_memory_entry: CacheEntry | None = None
            async with self._lock:
                cached = self._memory.get(key)
                if cached is not None:
                    candidate = CacheEntry(value=cached[1], expires_at=float(cached[0]))
                    if candidate.is_fresh(now):
                        self._memory.move_to_end(key)
                        return candidate
                    stale_memory_entry = candidate
            if stale_memory_entry is not None:
                # A stale in-memory value must not hide a newer durable write
                # from another process.  Drop it before consulting SQLite.
                # Disabled persistence has no durable source, so an explicit
                # stale read may still use the last in-memory value.
                if self._db is None and allow_stale:
                    return stale_memory_entry
                async with self._lock:
                    if self._memory.get(key) == cached:
                        self._memory.pop(key, None)

        async with self._db_lock:
            db = self._db
            if db is None:
                return None

            async with db.execute(
                "SELECT expires_at, payload FROM cache_entries WHERE cache_key = ?",
                (key,),
            ) as cursor:
                row = await cursor.fetchone()
            if not row:
                return None

            expires_at, payload = row
            entry = CacheEntry(value=orjson.loads(payload), expires_at=float(expires_at))
            fresh = entry.is_fresh(now)
            if not fresh and not allow_stale:
                return None
            if memory and fresh:
                async with self._lock:
                    self._remember_memory(key, entry.expires_at, entry.value, now=now)
            return entry

    async def get_stale(self, key: str, *, memory: bool = True) -> CacheEntry | None:
        """Return a value even when expired, preserving its original expiry."""

        return await self.get_entry(key, memory=memory, allow_stale=True)

    async def get(
        self,
        key: str,
        *,
        memory: bool = True,
        allow_stale: bool = False,
    ) -> tuple[Any | None, bool]:
        entry = await self.get_entry(key, memory=memory, allow_stale=allow_stale)
        if entry is None:
            return None, False
        return entry.value, entry.is_fresh()

    async def set(
        self,
        key: str,
        value: Any,
        ttl_seconds: int,
        *,
        memory: bool = True,
    ) -> None:
        expires_at = time.time() + ttl_seconds
        payload = _to_jsonable(value)
        encoded = orjson.dumps(payload).decode()
        async with self._db_lock:
            db = self._db
            if db is not None:
                async with sqlite_transaction(db, self.sqlite_path):
                    await db.execute(
                        """
                        INSERT INTO cache_entries (cache_key, expires_at, payload)
                        VALUES (?, ?, ?)
                        ON CONFLICT(cache_key) DO UPDATE SET
                            expires_at = excluded.expires_at,
                            payload = excluded.payload
                        """,
                        (key, expires_at, encoded),
                    )
                if memory:
                    async with self._lock:
                        self._remember_memory(key, expires_at, value)
                return
        if memory:
            async with self._lock:
                self._remember_memory(key, expires_at, value)

    async def invalidate(self, prefix: str | None = None) -> None:
        async with self._db_lock:
            db = self._db
            if db is not None:
                async with sqlite_transaction(db, self.sqlite_path):
                    if prefix is None:
                        await db.execute("DELETE FROM cache_entries")
                    else:
                        await db.execute(
                            "DELETE FROM cache_entries WHERE cache_key LIKE ?",
                            (f"{prefix}%",),
                        )
                async with self._lock:
                    self._remove_memory(prefix)
                return
        async with self._lock:
            self._remove_memory(prefix)

    async def cleanup_expired(self, *, limit: int = 100) -> int:
        """Bounded maintenance pass for expired rows; reads never delete rows."""

        if limit <= 0:
            raise ValueError("limit must be positive")
        now = time.time()
        deleted = 0
        async with self._db_lock:
            db = self._db
            if db is not None:
                async with sqlite_transaction(db, self.sqlite_path):
                    async with db.execute(
                        """
                        SELECT cache_key FROM cache_entries
                        WHERE expires_at <= ?
                        ORDER BY expires_at, cache_key
                        LIMIT ?
                        """,
                        (now, limit),
                    ) as cursor:
                        keys = [str(row[0]) for row in await cursor.fetchall()]
                    if keys:
                        await db.executemany(
                            "DELETE FROM cache_entries WHERE cache_key = ?",
                            [(key,) for key in keys],
                        )
                        deleted = len(keys)
                    async with self._lock:
                        self._remove_expired_memory(now, limit)
                    return deleted
        async with self._lock:
            return self._remove_expired_memory(now, limit)

    def _remove_memory(self, prefix: str | None) -> None:
        if prefix is None:
            self._memory.clear()
            return
        for key in list(self._memory):
            if key.startswith(prefix):
                self._memory.pop(key, None)

    def _remember_memory(
        self,
        key: str,
        expires_at: float,
        value: Any,
        *,
        now: float | None = None,
    ) -> None:
        """Store one fresh value and enforce expiry-first LRU bounds.

        The lock held by callers serializes updates.  Since every insertion
        leaves at most ``memory_cache_max_entries`` values, the expiry pass is
        bounded by that configured cap instead of growing with durable rows.
        """

        current_time = time.time() if now is None else now
        # Keep expired values available to explicit stale readers until the
        # cache is under capacity pressure.  Once an insertion would exceed
        # the cap, remove expired entries before falling back to LRU eviction.
        if key not in self._memory and len(self._memory) >= self.memory_cache_max_entries:
            for candidate_key, (candidate_expiry, _candidate_value) in list(self._memory.items()):
                if candidate_expiry <= current_time:
                    self._memory.pop(candidate_key, None)
        self._memory[key] = (expires_at, value)
        self._memory.move_to_end(key)
        while len(self._memory) > self.memory_cache_max_entries:
            self._memory.popitem(last=False)

    def _remove_expired_memory(self, now: float, limit: int) -> int:
        keys = [
            key
            for key, (expires_at, _value) in sorted(
                self._memory.items(), key=lambda item: (item[1][0], item[0])
            )
            if expires_at <= now
        ][:limit]
        for key in keys:
            self._memory.pop(key, None)
        return len(keys)


__all__ = ["CACHE_MAINTENANCE_BATCH_SIZE", "CacheEntry", "CacheStore"]
