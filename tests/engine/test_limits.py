"""FifoLimiter: strict FIFO hand-off, positions, cancellation safety. Waiting never fails."""

from __future__ import annotations

import asyncio

import pytest

from engine.jobs.limits import FifoLimiter

pytestmark = pytest.mark.asyncio


async def test_fifo_order_and_capacity():
    lim = FifoLimiter("render", 2)
    order: list[int] = []
    active = 0
    peak = 0

    async def worker(i: int):
        nonlocal active, peak
        async with lim.acquire():
            active += 1
            peak = max(peak, active)
            order.append(i)
            await asyncio.sleep(0.01)
            active -= 1

    tasks = []
    for i in range(8):
        tasks.append(asyncio.create_task(worker(i)))
        await asyncio.sleep(0)  # deterministic arrival order
    await asyncio.gather(*tasks)
    assert peak == 2
    assert order == list(range(8))
    assert lim.stats() == {"capacity": 2, "active": 0, "waiting": 0}


async def test_positions_reported_and_cleared():
    lim = FifoLimiter("whisper", 1)
    positions: dict[str, list] = {"b": [], "c": []}
    release = asyncio.Event()

    async def holder():
        async with lim.acquire():
            await release.wait()

    async def waiter(name: str):
        async with lim.acquire(lambda p: positions[name].append(p)):
            pass

    h = asyncio.create_task(holder())
    await asyncio.sleep(0)
    b = asyncio.create_task(waiter("b"))
    await asyncio.sleep(0)
    c = asyncio.create_task(waiter("c"))
    await asyncio.sleep(0.01)
    assert positions["b"][-1] == 1 and positions["c"][-1] == 2
    release.set()
    await asyncio.gather(h, b, c)
    assert positions["b"][-1] is None and positions["c"][-1] is None
    assert 1 in positions["c"]  # moved up the line


async def test_cancelled_waiter_does_not_leak_slot():
    lim = FifoLimiter("render", 1)
    release = asyncio.Event()

    async def holder():
        async with lim.acquire():
            await release.wait()

    h = asyncio.create_task(holder())
    await asyncio.sleep(0)

    async def waiter():
        async with lim.acquire():
            pass

    w = asyncio.create_task(waiter())
    await asyncio.sleep(0.01)
    w.cancel()
    with pytest.raises(asyncio.CancelledError):
        await w
    release.set()
    await h
    # slot must be free again
    async with lim.acquire():
        assert lim.stats()["active"] == 1
    assert lim.stats() == {"capacity": 1, "active": 0, "waiting": 0}


async def test_unlimited():
    lim = FifoLimiter("fal", None)
    async with lim.acquire(), lim.acquire(), lim.acquire():
        assert lim.stats()["waiting"] == 0
