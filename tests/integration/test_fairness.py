"""Gate 9: fairness under load. One noisy company must not push quiet companies past their
latency target (shard queues + round-robin dequeue + per-company caps + token buckets)."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict

import pytest

from framework_sanic.bootstrap import startup
from framework_sanic.core.domain.records import Lane
from framework_sanic.core.ports.queue import TOPIC_SYNC, QueueMessage
from framework_sanic.features.call_logging.models import CallActivity
from framework_sanic.testing import build_harness

pytestmark = pytest.mark.load

NOISY = 1500
QUIET_COMPANIES = 5
PER_QUIET = 10
LATENCY = 0.002  # simulated connector latency per call


@pytest.mark.parametrize("shard_count", [1, 4])
async def test_noisy_company_does_not_starve_others(shard_count: int) -> None:
    h = build_harness(
        SHARD_COUNT=shard_count,
        WORKER_CONCURRENCY=16,
        PER_COMPANY_CONCURRENCY=4,
        CONNECTOR_RATE_LIMIT_PER_MINUTE=10**6,
    )
    await startup(h.services, schedules=False)
    companies = ["noisy"] + [f"quiet{i}" for i in range(QUIET_COMPANIES)]
    for c in companies:
        await h.connect_company(c, external=True)
    h.connector.latency = LATENCY

    done_at: dict[str, list[float]] = defaultdict(list)
    original = h.connector.upsert_call

    async def tracked(cred, activity, existing):  # type: ignore[no-untyped-def]
        ref = await original(cred, activity, existing)
        done_at[activity.company_id].append(time.monotonic())
        return ref

    h.connector.upsert_call = tracked  # type: ignore[method-assign]

    async def seed(company: str, n: int) -> None:
        for i in range(n):
            act = CallActivity(
                company_id=company,
                call_id=f"{company}-{i}",
                state="hangup",
                terminal=True,
                expires_at=time.time() + 3600,
            )
            await h.repo.upsert(CallActivity.KIND, act.key(), lambda cur, a=act: a.to_record())
            await h.queue.publish(
                QueueMessage(
                    message_id=f"{company}-{i}",
                    topic=TOPIC_SYNC,
                    company_id=company,
                    lane=Lane.LIVE,
                    job="sync.call",
                    payload={"call_id": f"{company}-{i}"},
                )
            )

    await seed("noisy", NOISY)  # the noisy company's backlog is enqueued first
    for i in range(QUIET_COMPANIES):
        await seed(f"quiet{i}", PER_QUIET)

    start = time.monotonic()
    stop = asyncio.Event()
    pool = next(p for p in h.workers.pools if p.topic == TOPIC_SYNC)
    runner = asyncio.create_task(pool.run_forever(stop, idle_seconds=0.01))
    while sum(len(v) for v in done_at.values()) < NOISY + QUIET_COMPANIES * PER_QUIET:
        await asyncio.sleep(0.02)
        assert time.monotonic() - start < 60, "load test timed out"
    stop.set()
    await runner

    total = max(max(v) for v in done_at.values()) - start
    noisy_done = max(done_at["noisy"]) - start
    quiet_done = max(max(done_at[f"quiet{i}"]) for i in range(QUIET_COMPANIES)) - start
    # Quiet companies finish while the noisy backlog is still being worked through.
    print(f"shards={shard_count} quiet_done={quiet_done:.3f}s noisy_done={noisy_done:.3f}s")
    assert quiet_done < 0.25 * noisy_done, (quiet_done, noisy_done, total)
    # The noisy company never exceeded its concurrency cap.
    assert pool.inflight["noisy"] == 0
