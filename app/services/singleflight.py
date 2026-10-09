import asyncio
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar, cast

T = TypeVar("T")


class SingleFlight:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._tasks: dict[str, asyncio.Task[Any]] = {}

    async def run(self, key: str, factory: Callable[[], Coroutine[Any, Any, T]]) -> T:
        async with self._lock:
            task = self._tasks.get(key)
            if task is None:
                task = asyncio.create_task(factory())
                self._tasks[key] = task
                task.add_done_callback(lambda completed: self._complete(key, completed))

        # A cancelled caller must not propagate cancellation to the shared task.
        # Cleanup is attached to the shared task itself so that a cancelled caller
        # cannot remove an in-flight task and cause duplicate provider work.
        return cast(T, await asyncio.shield(task))

    def _complete(self, key: str, task: asyncio.Task[Any]) -> None:
        if self._tasks.get(key) is task:
            self._tasks.pop(key, None)

        # If every waiter was cancelled, consume a failed task's exception so the
        # event loop does not report an unhandled-task warning. Waiters still see
        # the original exception when they await the shared task.
        if not task.cancelled():
            task.exception()
