"""Queue contract suite (memory adapter; the Pub/Sub adapter runs it against the emulator)."""

from __future__ import annotations

import asyncio
import os

import pytest

from framework_sanic.adapters.queue.memory import MemoryQueue
from framework_sanic.core.domain.records import Lane, shard_for
from framework_sanic.core.ports.queue import Queue, QueueMessage


@pytest.fixture(
    params=[
        "memory",
        pytest.param(
            "pubsub",
            marks=pytest.mark.skipif(
                not os.environ.get("PUBSUB_EMULATOR_HOST"), reason="PUBSUB_EMULATOR_HOST not set"
            ),
        ),
    ]
)
async def queue(request: pytest.FixtureRequest) -> Queue:
    if request.param == "memory":
        return MemoryQueue(shard_count=4)
    from framework_sanic.adapters.queue.pubsub import PubSubQueue

    q = PubSubQueue("contract-test", prefix=f"t{os.getpid()}", shard_count=4, topics=["t"])
    await q.ensure_resources()
    return q


def msg(company: str, n: int, lane: Lane = Lane.LIVE, topic: str = "t") -> QueueMessage:
    return QueueMessage(
        message_id=f"{company}-{n}", topic=topic, company_id=company, lane=lane, job="j", payload={"n": n}
    )


ALL = list(range(4))


async def test_publish_pull_ack(queue: Queue) -> None:
    await queue.publish(msg("c1", 1))
    got = await queue.pull("t", ALL, 10)
    assert [d.message.message_id for d in got] == ["c1-1"]
    assert got[0].shard == shard_for("c1", 4)
    await queue.ack(got[0])
    assert await queue.pull("t", ALL, 10) == []


async def test_shards_isolate(queue: Queue) -> None:
    await queue.publish(msg("c1", 1))
    s = shard_for("c1", 4)
    assert await queue.pull("t", [x for x in ALL if x != s], 10) == []
    assert len(await queue.pull("t", [s], 10)) == 1


async def test_lanes_highest_first(queue: Queue) -> None:
    await queue.publish(msg("c1", 1, Lane.BACKFILL))
    await queue.publish(msg("c1", 2, Lane.LIVE))
    await queue.publish(msg("c1", 3, Lane.UI))
    got = await queue.pull("t", ALL, 10)
    assert [d.message.lane for d in got] == [Lane.UI, Lane.LIVE, Lane.BACKFILL]


async def test_nack_redelivers_after_delay_and_counts_attempts(queue: Queue) -> None:
    await queue.publish(msg("c1", 1))
    (d,) = await queue.pull("t", ALL, 10)
    await queue.nack(d, 0.05)
    assert await queue.pull("t", ALL, 10) == []
    await asyncio.sleep(0.08)
    (d2,) = await queue.pull("t", ALL, 10)
    assert d2.message.attempt == 1
    await queue.nack(d2, 0, count_attempt=False)
    (d3,) = await queue.pull("t", ALL, 10)
    assert d3.message.attempt == 1


async def test_round_robin_across_companies(queue: Queue) -> None:
    if not isinstance(queue, MemoryQueue):
        pytest.skip("ordering across companies is only guaranteed by the memory adapter")
    q = MemoryQueue(shard_count=1)
    for i in range(10):
        await q.publish(msg("noisy", i))
    await q.publish(msg("quiet1", 0))
    await q.publish(msg("quiet2", 0))
    got = await q.pull("t", [0], 3)
    assert {d.message.company_id for d in got} == {"noisy", "quiet1", "quiet2"}


async def test_unacked_messages_are_redelivered_after_visibility_timeout() -> None:
    q = MemoryQueue(shard_count=1, visibility_timeout=0.05)
    await q.publish(msg("c1", 1))
    assert len(await q.pull("t", [0], 1)) == 1
    await asyncio.sleep(0.07)
    assert len(await q.pull("t", [0], 1)) == 1


async def test_stats_report_oldest_age() -> None:
    q = MemoryQueue(shard_count=2)
    await q.publish(msg("c1", 1))
    await asyncio.sleep(0.02)
    st = await q.stats("t")
    assert st.depth == 1 and st.oldest_age_seconds >= 0.02
