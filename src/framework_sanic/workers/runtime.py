"""Worker runtime: pulls from sharded topics and dispatches to feature job handlers.

Fairness: the queue hands out messages highest lane first and round-robin across companies;
on top of that each pool caps in-flight messages per company, so one noisy company cannot
occupy all worker slots. Over-cap messages are deferred without counting an attempt.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import Counter
from collections.abc import Sequence

from framework_sanic.core.features.jobs import JobHandler, JobResult, backoff, deadletter, retry
from framework_sanic.core.ports.queue import TOPIC_DEADLETTER, Delivery, Queue

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 30
DEFER_SECONDS = 0.05


class WorkerPool:
    def __init__(
        self,
        queue: Queue,
        topic: str,
        handlers: dict[str, JobHandler],
        *,
        shards: Sequence[int],
        concurrency: int = 32,
        per_company_cap: int = 4,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        self.queue = queue
        self.topic = topic
        self.handlers = handlers
        self.shards = list(shards)
        self.concurrency = concurrency
        self.per_company_cap = per_company_cap
        self.max_attempts = max_attempts
        self.inflight: Counter[str] = Counter()
        self.processed = 0

    async def _process(self, d: Delivery) -> None:
        msg = d.message
        handler = self.handlers.get(msg.job)
        try:
            if handler is None:
                result = deadletter(f"no handler for job {msg.job!r} on topic {self.topic}")
            else:
                result = await handler(msg)
        except Exception as exc:  # noqa: BLE001
            log.exception("job %s for %s failed (attempt %s)", msg.job, msg.company_id, msg.attempt)
            result = retry(backoff(msg.attempt), repr(exc))
        await self._apply(d, result)
        self.processed += 1

    async def _apply(self, d: Delivery, result: JobResult) -> None:
        msg = d.message
        if result.action == "retry" and result.count_attempt and msg.attempt + 1 >= self.max_attempts:
            result = deadletter(f"max attempts exceeded: {result.reason}")
        if result.action == "ack":
            await self.queue.ack(d)
        elif result.action == "retry":
            await self.queue.nack(d, result.delay, count_attempt=result.count_attempt)
        else:
            log.error("dead-lettering %s %s: %s", msg.job, msg.message_id, result.reason)
            await self.queue.publish(
                msg.model_copy(
                    update={
                        "topic": TOPIC_DEADLETTER,
                        "payload": {**msg.payload, "_reason": result.reason, "_topic": msg.topic},
                    }
                )
            )
            await self.queue.ack(d)

    async def _admit(self, deliveries: list[Delivery]) -> list[Delivery]:
        admitted: list[Delivery] = []
        for d in deliveries:
            company = d.message.company_id
            if self.inflight[company] >= self.per_company_cap:
                await self.queue.nack(d, DEFER_SECONDS, count_attempt=False)
                continue
            self.inflight[company] += 1
            admitted.append(d)
        return admitted

    async def _run_tracked(self, d: Delivery) -> None:
        try:
            await self._process(d)
        finally:
            self.inflight[d.message.company_id] -= 1

    async def run_once(self) -> int:
        """Pull one batch, process it concurrently, return the number of handled messages."""
        deliveries = await self.queue.pull(self.topic, self.shards, self.concurrency)
        admitted = await self._admit(deliveries)
        await asyncio.gather(*(self._run_tracked(d) for d in admitted))
        return len(admitted)

    async def run_forever(self, stop: asyncio.Event, *, idle_seconds: float = 0.25) -> None:
        tasks: set[asyncio.Task[None]] = set()
        while not stop.is_set():
            free = self.concurrency - len(tasks)
            if free <= 0:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                continue
            try:
                deliveries = await self.queue.pull(self.topic, self.shards, free)
            except Exception:  # noqa: BLE001
                log.exception("pull from %s failed", self.topic)
                await asyncio.sleep(1.0)
                continue
            if not deliveries:
                wait = getattr(self.queue, "wait_for_messages", None)
                if wait is not None:
                    await wait(idle_seconds)
                else:
                    await asyncio.sleep(idle_seconds)
                continue
            admitted = await self._admit(deliveries)
            for d in admitted:
                task = asyncio.create_task(self._run_tracked(d))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            if not admitted:
                # Everything pulled was over its company's cap: wait for a slot instead of spinning.
                if tasks:
                    await asyncio.wait(tasks, timeout=DEFER_SECONDS, return_when=asyncio.FIRST_COMPLETED)
                else:
                    await asyncio.sleep(DEFER_SECONDS)
            else:
                await asyncio.sleep(0)  # always yield to running handlers
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


class WorkerRuntime:
    """All pools for one process (stage 1, stage 2, control)."""

    def __init__(self, pools: list[WorkerPool]) -> None:
        self.pools = pools
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        self._stop.clear()
        self._tasks = [
            asyncio.create_task(p.run_forever(self._stop), name=f"worker:{p.topic}") for p in self.pools
        ]

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(t), timeout=5)
            t.cancel()
        self._tasks.clear()

    async def drain(self, *, idle_timeout: float = 0.3, max_rounds: int = 10_000) -> int:
        """Test helper: run pools until nothing is processed for ``idle_timeout`` seconds.

        Short deferrals (per-company cap) become visible again within that window; long
        delayed retries (parking, backoff) are left in the queue.
        """
        import time

        total = 0
        idle_since: float | None = None
        for _ in range(max_rounds):
            handled = 0
            for p in self.pools:
                handled += await p.run_once()
            total += handled
            if handled:
                idle_since = None
                continue
            now = time.monotonic()
            idle_since = idle_since or now
            if now - idle_since >= idle_timeout:
                return total
            await asyncio.sleep(DEFER_SECONDS)
        return total
