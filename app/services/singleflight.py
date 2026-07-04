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
        try:
            return cast(T, await task)
        finally:
            async with self._lock:
                if self._tasks.get(key) is task:
                    self._tasks.pop(key, None)
