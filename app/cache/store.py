import asyncio
import time
from pathlib import Path
from typing import Any

import aiosqlite
import orjson
from pydantic import BaseModel


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _to_jsonable(item) for key, item in value.items()}
    return value


class CacheStore:
    def __init__(self, *, sqlite_enabled: bool, sqlite_path: Path) -> None:
        self.sqlite_enabled = sqlite_enabled
        self.sqlite_path = sqlite_path
        self._memory: dict[str, tuple[float, Any]] = {}
        self._lock = asyncio.Lock()
        self._db: aiosqlite.Connection | None = None

    async def startup(self) -> None:
        if not self.sqlite_enabled:
            return
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.sqlite_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS cache_entries (
                cache_key TEXT PRIMARY KEY,
                expires_at REAL NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def get(self, key: str) -> tuple[Any | None, bool]:
        now = time.time()
        async with self._lock:
            entry = self._memory.get(key)
            if entry and entry[0] > now:
                return entry[1], True
            if entry:
                self._memory.pop(key, None)

        if self._db is None:
            return None, False

        async with self._db.execute(
            "SELECT expires_at, payload FROM cache_entries WHERE cache_key = ?",
            (key,),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None, False
        expires_at, payload = row
        if float(expires_at) <= now:
            await self._db.execute("DELETE FROM cache_entries WHERE cache_key = ?", (key,))
            await self._db.commit()
            return None, False

        value = orjson.loads(payload)
        async with self._lock:
            self._memory[key] = (float(expires_at), value)
        return value, True

    async def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        expires_at = time.time() + ttl_seconds
        payload = _to_jsonable(value)
        async with self._lock:
            self._memory[key] = (expires_at, value)

        if self._db is None:
            return

        await self._db.execute(
            """
            INSERT INTO cache_entries (cache_key, expires_at, payload)
            VALUES (?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                expires_at = excluded.expires_at,
                payload = excluded.payload
            """,
            (key, expires_at, orjson.dumps(payload).decode()),
        )
        await self._db.commit()

    async def invalidate(self, prefix: str | None = None) -> None:
        async with self._lock:
            if prefix is None:
                self._memory.clear()
            else:
                for key in list(self._memory):
                    if key.startswith(prefix):
                        self._memory.pop(key, None)

        if self._db is None:
            return

        if prefix is None:
            await self._db.execute("DELETE FROM cache_entries")
        else:
            await self._db.execute(
                "DELETE FROM cache_entries WHERE cache_key LIKE ?",
                (f"{prefix}%",),
            )
        await self._db.commit()
