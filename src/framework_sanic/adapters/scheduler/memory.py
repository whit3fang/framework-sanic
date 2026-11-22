"""In-process scheduler (asyncio tasks). In production, Cloud Scheduler can drive the same jobs."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)


class AsyncioScheduler:
    def __init__(self) -> None:
        self._jobs: list[tuple[str, float, Callable[[], Awaitable[None]], float]] = []
        self._tasks: list[asyncio.Task[None]] = []

    def every(
        self, name: str, interval_seconds: float, fn: Callable[[], Awaitable[None]], *, jitter: float = 0.0
    ) -> None:
        self._jobs.append((name, interval_seconds, fn, jitter))

    async def _loop(
        self, name: str, interval: float, fn: Callable[[], Awaitable[None]], jitter: float
    ) -> None:
        await asyncio.sleep(random.uniform(0, jitter) if jitter else 0)
        while True:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scheduled job %s failed", name)
            await asyncio.sleep(interval + (random.uniform(0, jitter) if jitter else 0))

    async def start(self) -> None:
        for name, interval, fn, jitter in self._jobs:
            self._tasks.append(
                asyncio.create_task(self._loop(name, interval, fn, jitter), name=f"sched:{name}")
            )

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
