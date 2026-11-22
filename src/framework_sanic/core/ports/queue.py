"""Durable queue port (Pub/Sub or Cloud Tasks in production, in-memory locally).

Topics are sharded by company hash (``shard_for``) and every message carries a priority lane.
Delivery is at-least-once; every consumer must be idempotent.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, Field

from ..domain.records import Lane

TOPIC_EVENTS = "events"  # stage 1
TOPIC_SYNC = "sync"  # stage 2
TOPIC_CONTROL = "control"  # reconcile and other control jobs
TOPIC_DEADLETTER = "deadletter"


class QueueMessage(BaseModel):
    message_id: str
    topic: str
    company_id: str
    lane: Lane = Lane.LIVE
    job: str
    payload: dict[str, Any] = Field(default_factory=dict)
    attempt: int = 0
    enqueued_at: float = 0.0


@dataclass
class Delivery:
    message: QueueMessage
    shard: int
    ack_id: str


@dataclass
class QueueStats:
    depth: int
    oldest_age_seconds: float


class Queue(Protocol):
    shard_count: int

    async def publish(self, message: QueueMessage) -> None: ...

    async def pull(self, topic: str, shards: Sequence[int], max_messages: int) -> list[Delivery]:
        """Pull up to ``max_messages``, highest lane first, round-robin across companies."""
        ...

    async def ack(self, delivery: Delivery) -> None: ...

    async def nack(self, delivery: Delivery, delay_seconds: float, *, count_attempt: bool = True) -> None:
        """Make the message visible again after ``delay_seconds``."""
        ...

    async def stats(self, topic: str) -> QueueStats:
        """Autoscaling signal: oldest-message age, not depth."""
        ...

    async def ping(self) -> bool: ...

    async def close(self) -> None: ...
