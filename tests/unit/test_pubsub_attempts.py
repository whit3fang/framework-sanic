"""Pub/Sub adapter: attempts and delays travel in the message (Pub/Sub's delivery_attempt cannot
tell a failure from a deferral). Uses in-memory stand-ins for the Pub/Sub clients."""

from __future__ import annotations

import concurrent.futures
import itertools
import time
from types import SimpleNamespace
from typing import Any

import pytest

from framework_sanic.core.domain.records import Lane
from framework_sanic.core.ports.queue import QueueMessage

pytest.importorskip("google.cloud.pubsub_v1")
from framework_sanic.adapters.queue.pubsub import PubSubQueue  # noqa: E402


class FakeClients:
    """One stand-in for publisher + subscriber: topic i feeds subscription i + "-sub"."""

    def __init__(self) -> None:
        self.messages: dict[str, list[tuple[str, bytes]]] = {}  # subscription -> [(ack_id, data)]
        self.deadlines: dict[str, int] = {}
        self.acked: list[str] = []
        self._ids = itertools.count()

    def topic_path(self, project: str, name: str) -> str:
        return name

    def subscription_path(self, project: str, name: str) -> str:
        return name

    def publish(self, topic: str, data: bytes) -> concurrent.futures.Future[str]:
        self.messages.setdefault(topic + "-sub", []).append((f"a{next(self._ids)}", data))
        done: concurrent.futures.Future[str] = concurrent.futures.Future()
        done.set_result("id")
        return done

    def pull(self, request: dict[str, Any], timeout: float) -> Any:
        sub = request["subscription"]
        ready = [(a, d) for a, d in self.messages.get(sub, []) if a not in self.deadlines]
        return SimpleNamespace(
            received_messages=[SimpleNamespace(ack_id=a, message=SimpleNamespace(data=d)) for a, d in ready]
        )

    def acknowledge(self, request: dict[str, Any]) -> None:
        for ack_id in request["ack_ids"]:
            self.acked.append(ack_id)
            sub = request["subscription"]
            self.messages[sub] = [(a, d) for a, d in self.messages[sub] if a != ack_id]

    def modify_ack_deadline(self, request: dict[str, Any]) -> None:
        for ack_id in request["ack_ids"]:
            self.deadlines[ack_id] = request["ack_deadline_seconds"]

    def release(self) -> None:
        """Every hidden message becomes visible again (time passes)."""
        self.deadlines.clear()


def queue() -> tuple[PubSubQueue, FakeClients]:
    fake = FakeClients()
    return PubSubQueue("p", prefix="t", shard_count=1, topics=["sync"], publisher=fake, subscriber=fake), fake


MSG = QueueMessage(message_id="m1", topic="sync", company_id="c1", lane=Lane.LIVE, job="j")


async def test_counted_retry_carries_the_attempt_and_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    q, fake = queue()
    await q.publish(MSG)
    (d,) = await q.pull("sync", [0], 10)
    await q.nack(d, 120.0)
    assert d.ack_id.split("|")[1] in fake.acked  # the original is gone; a copy carries the retry
    assert await q.pull("sync", [0], 10) == []  # the copy is held back until not_before
    assert list(fake.deadlines.values()) == [120]
    fake.release()
    assert await q.pull("sync", [0], 10) == []  # redelivered early: held back again
    later = time.time() + 121
    monkeypatch.setattr("framework_sanic.adapters.queue.pubsub.time.time", lambda: later)
    fake.release()
    (again,) = await q.pull("sync", [0], 10)
    assert again.message.attempt == 1


async def test_uncounted_deferral_keeps_the_attempt() -> None:
    q, fake = queue()
    await q.publish(MSG.model_copy(update={"attempt": 3}))
    (d,) = await q.pull("sync", [0], 10)
    await q.nack(d, 0.05, count_attempt=False)
    assert fake.acked == [] and list(fake.deadlines.values()) == [1]  # rounded up: never a 0 s spin
    fake.release()
    (again,) = await q.pull("sync", [0], 10)
    assert again.message.attempt == 3


async def test_long_uncounted_delay_is_not_cut_to_ten_minutes() -> None:
    q, fake = queue()
    await q.publish(MSG)
    (d,) = await q.pull("sync", [0], 10)
    await q.nack(d, 3600.0, count_attempt=False)
    ((_, data),) = fake.messages["t-sync-live-0-sub"]
    copy = QueueMessage.model_validate_json(data)
    assert copy.attempt == 0 and copy.not_before is not None and copy.not_before > time.time() + 3500
