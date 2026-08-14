from app.services.bounded_cache import BoundedMap, BoundedTTLCache


def test_bounded_map_rejects_invalid_capacity() -> None:
    try:
        BoundedMap[str, int](0)
    except ValueError as error:
        assert str(error) == "max_entries must be positive"
    else:
        raise AssertionError("invalid capacity was accepted")


def test_bounded_map_updates_entries_and_evicts_the_oldest_key() -> None:
    values = BoundedMap[str, int](2)
    values.set("first", 1)
    values.set("second", 2)
    values.set("first", 3)
    values.set("third", 4)

    assert values.get("first") is None
    assert list(values.items()) == [("second", 2), ("third", 4)]
    assert list(values.values()) == [2, 4]
    assert len(values) == 2


def test_ttl_cache_rejects_invalid_capacity() -> None:
    try:
        BoundedTTLCache[str, int](0)
    except ValueError as error:
        assert str(error) == "max_entries must be positive"
    else:
        raise AssertionError("invalid capacity was accepted")


def test_ttl_cache_expires_entries_and_uses_lru_eviction() -> None:
    cache = BoundedTTLCache[str, int](2)
    cache.set("oldest", 10.0, 1)
    cache.set("recent", 10.0, 2)

    assert cache.get("oldest", 1.0) == (True, 1)
    cache.set("new", 10.0, 3)

    assert cache.get("recent", 1.0) == (False, None)
    assert cache.get("oldest", 10.0) == (False, None)
    assert cache.get("missing", 1.0) == (False, None)
    assert cache.get("new", 1.0) == (True, 3)
    assert len(cache) == 1
