"""Scheduler, ChangeLogSource and Clock ports."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Protocol

from pydantic import BaseModel


class Scheduler(Protocol):
    def every(
        self, name: str, interval_seconds: float, fn: Callable[[], Awaitable[None]], *, jitter: float = 0.0
    ) -> None: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class ChangeEvent(BaseModel):
    action: str  # e.g. "target_created", "target_deleted"
    target_type: str | None = None
    target_id: str | None = None


class ChangeLogSource(Protocol):
    """Placeholder port: the Dialpad change-log API shape and scopes are unknown (VERIFY)."""

    async def poll(self, company_id: str, cursor: str | None) -> tuple[list[ChangeEvent], str | None]: ...


class Clock(Protocol):
    def now(self) -> float: ...


class SystemClock:
    def now(self) -> float:
        return time.time()
