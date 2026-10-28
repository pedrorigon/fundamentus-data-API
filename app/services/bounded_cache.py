from __future__ import annotations

from collections import OrderedDict
from collections.abc import ItemsView, ValuesView


class BoundedMap[K, V]:
    """Insertion-ordered map that discards its oldest key at capacity."""

    def __init__(self, max_entries: int) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self._max_entries = max_entries
        self._values: dict[K, V] = {}

    def get(self, key: K) -> V | None:
        return self._values.get(key)

    def set(self, key: K, value: V) -> None:
        if key not in self._values and len(self._values) >= self._max_entries:
            self._values.pop(next(iter(self._values)))
        self._values[key] = value

    def values(self) -> ValuesView[V]:
        return self._values.values()

    def items(self) -> ItemsView[K, V]:
        return self._values.items()

    def __len__(self) -> int:
        return len(self._values)


class BoundedTTLCache[K, V]:
    """LRU cache whose entries also expire at an absolute monotonic time."""

    def __init__(self, max_entries: int) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self._max_entries = max_entries
        self._values: OrderedDict[K, tuple[float, V]] = OrderedDict()

    def get(self, key: K, now: float) -> tuple[bool, V | None]:
        cached = self._values.get(key)
        if cached is None:
            return False, None
        expires_at, value = cached
        if expires_at <= now:
            self._values.pop(key, None)
            return False, None
        self._values.move_to_end(key)
        return True, value

    def set(self, key: K, expires_at: float, value: V) -> None:
        self._values[key] = (expires_at, value)
        self._values.move_to_end(key)
        while len(self._values) > self._max_entries:
            self._values.popitem(last=False)

    def __len__(self) -> int:
        return len(self._values)
