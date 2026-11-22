"""Test feature (``FEATURES=tests.support.slow_jobs``): a sync-topic job that takes ``LATENCY``
seconds, standing in for a slow external call, and records when each company's jobs finish."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict

from framework_sanic.core.features.context import FeatureContext
from framework_sanic.core.features.jobs import ACK, JobResult
from framework_sanic.core.ports.queue import TOPIC_SYNC, QueueMessage

LATENCY = 0.002
done_at: dict[str, list[float]] = defaultdict(list)


class SlowJobs:
    name = "test.slow_jobs"

    def setup(self, ctx: FeatureContext) -> None:
        ctx.job(TOPIC_SYNC, "test.slow")(self.slow)

    async def slow(self, msg: QueueMessage) -> JobResult:
        await asyncio.sleep(LATENCY)
        done_at[msg.company_id].append(time.monotonic())
        return ACK


feature = SlowJobs()
