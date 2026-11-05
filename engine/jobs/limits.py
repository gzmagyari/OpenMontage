"""FIFO resource limits.

A limit is a *waiting line*, never a rejection: when every slot of a resource
(render, whisper, mix, ...) is busy, the job waits its turn and reports its
position. Slots are handed directly to the next waiter on release, so order is
strictly first-in-first-out and nothing can barge ahead.
"""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Callable, Optional

PositionCallback = Callable[[Optional[int]], None]


@dataclass
class _Waiter:
    future: asyncio.Future
    on_position: PositionCallback | None


class FifoLimiter:
    def __init__(self, name: str, capacity: int | None):
        if capacity is not None and capacity < 1:
            raise ValueError(f"limit {name!r} capacity must be >= 1 (or None for unlimited)")
        self.name = name
        self.capacity = capacity
        self._active = 0
        self._waiters: deque[_Waiter] = deque()

    def stats(self) -> dict:
        return {"capacity": self.capacity, "active": self._active, "waiting": len(self._waiters)}

    @asynccontextmanager
    async def acquire(self, on_position: PositionCallback | None = None) -> AsyncIterator[None]:
        await self._acquire(on_position)
        try:
            yield
        finally:
            self._release()

    async def _acquire(self, on_position: PositionCallback | None) -> None:
        if self.capacity is None or (self._active < self.capacity and not self._waiters):
            self._active += 1
            return
        waiter = _Waiter(asyncio.get_running_loop().create_future(), on_position)
        self._waiters.append(waiter)
        self._notify()
        try:
            await waiter.future
        except asyncio.CancelledError:
            if waiter.future.done() and not waiter.future.cancelled():
                # The slot was handed to us just before cancellation: give it back.
                self._release()
            else:
                try:
                    self._waiters.remove(waiter)
                except ValueError:
                    pass
                self._notify()
            raise
        finally:
            if on_position is not None:
                on_position(None)

    def _release(self) -> None:
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.future.done():
                waiter.future.set_result(True)  # slot transfers; active count unchanged
                self._notify()
                return
        self._active -= 1

    def _notify(self) -> None:
        for position, waiter in enumerate(self._waiters, start=1):
            if waiter.on_position is not None:
                try:
                    waiter.on_position(position)
                except Exception:
                    pass


class Limits:
    """Named limiters, sized from Settings."""

    def __init__(self, settings) -> None:
        self._limiters: dict[str, FifoLimiter] = {
            "render": FifoLimiter("render", settings.render_concurrency),
            "tool": FifoLimiter("tool", max(1, settings.max_active_jobs)),
            "agent": FifoLimiter("agent", max(1, getattr(settings, "agent_concurrency",
                                                         settings.max_active_jobs))),
        }

    def get(self, name: str) -> FifoLimiter:
        return self._limiters[name]

    def stats(self) -> dict:
        return {name: lim.stats() for name, lim in self._limiters.items()}
