"""Shared SQLite coordination for stores using the cache database.

The cache and income event stores intentionally use separate SQLite connections,
but they still need one process-wide writer lock when they point at the same
database.  SQLite's busy timeout covers writers in another process; this lock
prevents avoidable contention between the two local connections.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Final, Self

import aiosqlite

SQLITE_BUSY_TIMEOUT_MS: Final[int] = 5_000
PATH_LOCK_RETRY_SECONDS: Final[float] = 0.001
_PATH_LOCKS: dict[str, threading.Lock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


class _AsyncPathLock:
    """Adapt a process-wide primitive lock to cancellable async code.

    ``asyncio.Lock`` is bound to an event loop and cannot coordinate stores that
    are used from two worker threads.  The underlying primitive lock has a
    non-blocking acquire, so cancellation never leaves a lock acquired by a
    background thread.  A short cooperative wait keeps contention off the
    event loop while preserving one lock for every normalized database path.
    """

    __slots__ = ("_lock", "_acquired")

    def __init__(self, lock: threading.Lock) -> None:
        self._lock = lock
        self._acquired = False

    async def __aenter__(self) -> Self:
        while not self._lock.acquire(blocking=False):  # noqa: ASYNC110
            # A zero-delay callback loop monopolizes the GIL when several
            # worker loops contend for the same path (coverage instrumentation
            # made that starvation deterministic). A bounded millisecond
            # backoff remains cancellable and lets the current owner finish
            # its SQLite commit or close operation.
            await asyncio.sleep(PATH_LOCK_RETRY_SECONDS)
        self._acquired = True
        return self

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        if self._acquired:
            self._acquired = False
            self._lock.release()


def sqlite_path_lock(path: Path) -> _AsyncPathLock:
    """Return the process-wide writer lock for *path*."""

    key = str(path.expanduser().resolve(strict=False))
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PATH_LOCKS[key] = lock
    return _AsyncPathLock(lock)


async def configure_sqlite_connection(db: aiosqlite.Connection) -> None:
    """Configure a connection without starting a transaction."""

    await db.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    await db.execute("PRAGMA journal_mode = WAL")
    await db.execute("PRAGMA synchronous = NORMAL")


async def _rollback(db: aiosqlite.Connection) -> None:
    """Finish rollback even when the caller is already being cancelled."""

    rollback_task = asyncio.create_task(db.rollback())
    try:
        await asyncio.shield(rollback_task)
    except asyncio.CancelledError as cancelled:
        # A shielded task keeps running after cancellation.  Await it once more
        # so the connection is not returned to the pool with an open write.
        try:
            await rollback_task
        except BaseException as rollback_error:
            raise rollback_error from cancelled
        raise


@asynccontextmanager
async def _transaction_body(db: aiosqlite.Connection) -> AsyncIterator[aiosqlite.Connection]:
    # Mark the transaction as needing cleanup before queuing BEGIN.  If the
    # task is cancelled while aiosqlite is processing BEGIN IMMEDIATE, the
    # worker may still open a transaction after cancellation is delivered.
    # Rolling back unconditionally leaves the connection in a known state and
    # is harmless when BEGIN itself failed before opening one.
    begun = True
    try:
        await db.execute("BEGIN IMMEDIATE")
        yield db
        await db.commit()
    except BaseException as error:
        if begun:
            try:
                await _rollback(db)
            except BaseException as rollback_error:
                error.add_note(f"SQLite rollback failed: {rollback_error!r}")
        raise


@asynccontextmanager
async def sqlite_transaction(
    db: aiosqlite.Connection,
    path: Path,
    *,
    acquire_lock: bool = True,
) -> AsyncIterator[aiosqlite.Connection]:
    """Run a complete serialized SQLite write transaction.

    ``BEGIN IMMEDIATE`` makes the write ownership explicit.  A failure or task
    cancellation always rolls the transaction back before the exception is
    propagated.  The shared path lock covers both stores in this process;
    SQLite's busy timeout handles a writer in another process.
    """

    if acquire_lock:
        async with sqlite_path_lock(path):
            async with _transaction_body(db) as transaction:
                yield transaction
        return

    async with _transaction_body(db) as transaction:
        yield transaction


__all__ = [
    "SQLITE_BUSY_TIMEOUT_MS",
    "configure_sqlite_connection",
    "sqlite_path_lock",
    "sqlite_transaction",
]
