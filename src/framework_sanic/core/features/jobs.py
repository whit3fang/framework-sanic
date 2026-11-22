"""Job handler contract used by workers."""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from ..ports.queue import QueueMessage


@dataclass(frozen=True)
class JobResult:
    action: Literal["ack", "retry", "deadletter"]
    delay: float = 0.0
    reason: str = ""
    count_attempt: bool = True


ACK = JobResult("ack")


def retry(delay: float, reason: str = "", *, count_attempt: bool = True) -> JobResult:
    return JobResult("retry", delay=max(0.0, delay), reason=reason, count_attempt=count_attempt)


def deadletter(reason: str) -> JobResult:
    return JobResult("deadletter", reason=reason)


def backoff(attempt: int, *, base: float = 1.0, cap: float = 600.0) -> float:
    """Exponential backoff with full jitter."""
    ceiling = min(cap, base * (2 ** min(attempt, 20)))
    return random.uniform(ceiling / 2, ceiling)


JobHandler = Callable[[QueueMessage], Awaitable[JobResult]]
