import asyncio

import pytest

from app.services.singleflight import SingleFlight


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_shared_task() -> None:
    flight = SingleFlight()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def factory() -> str:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return "shared-result"

    first = asyncio.create_task(flight.run("asset", factory))
    await started.wait()
    second = asyncio.create_task(flight.run("asset", factory))
    await asyncio.sleep(0)

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    assert calls == 1

    release.set()
    assert await second == "shared-result"
    assert await flight.run("asset", factory) == "shared-result"
    assert calls == 2


@pytest.mark.asyncio
async def test_failed_shared_task_is_cleaned_up_after_all_waiters_cancel() -> None:
    flight = SingleFlight()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def factory() -> str:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        raise RuntimeError("provider failed")

    waiter = asyncio.create_task(flight.run("asset", factory))
    await started.wait()
    shared = flight._tasks["asset"]
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    release.set()
    with pytest.raises(RuntimeError, match="provider failed"):
        await shared
    assert "asset" not in flight._tasks

    replacement = asyncio.create_task(flight.run("asset", factory))
    with pytest.raises(RuntimeError, match="provider failed"):
        await replacement
    assert calls == 2
