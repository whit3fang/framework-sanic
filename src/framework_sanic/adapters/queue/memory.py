"""In-memory sharded queue with priority lanes and per-company round-robin.

Not durable across process restarts: for tests and single-process local development only.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Sequence
from dataclasses import dataclass

from framework_sanic.core.domain.errors import ServiceUnavailable
from framework_sanic.core.domain.records import LANE_ORDER, Lane, shard_for
from framework_sanic.core.ports.queue import Delivery, QueueMessage, QueueStats


@dataclass
class _Entry:
    message: QueueMessage
    first_enqueued: float


class _Shard:
    def __init__(self) -> None:
        # lane -> company -> deque of entries (OrderedDict gives round-robin order)
        self.ready: dict[Lane, OrderedDict[str, deque[_Entry]]] = {lane: OrderedDict() for lane in LANE_ORDER}
        self.delayed: list[tuple[float, int, _Entry]] = []

    def push(self, entry: _Entry) -> None:
        lane = self.ready[entry.message.lane]
        lane.setdefault(entry.message.company_id, deque()).append(entry)

    def promote(self, now: float) -> None:
        while self.delayed and self.delayed[0][0] <= now:
            _, _, entry = heapq.heappop(self.delayed)
            self.push(entry)

    def pop(self) -> _Entry | None:
        for lane in LANE_ORDER:
            companies = self.ready[lane]
            if not companies:
                continue
            company, entries = next(iter(companies.items()))
            entry = entries.popleft()
            companies.pop(company)
            if entries:
                companies[company] = entries  # move to the back: round-robin across companies
            return entry
        return None

    def depth_and_oldest(self) -> tuple[int, float | None]:
        depth = 0
        oldest: float | None = None
        for lane in self.ready.values():
            for entries in lane.values():
                depth += len(entries)
                for e in entries:
                    oldest = e.first_enqueued if oldest is None else min(oldest, e.first_enqueued)
        for _, _, e in self.delayed:
            depth += 1
            oldest = e.first_enqueued if oldest is None else min(oldest, e.first_enqueued)
        return depth, oldest


class MemoryQueue:
    def __init__(self, shard_count: int = 1, *, visibility_timeout: float = 300.0) -> None:
        self.shard_count = shard_count
        self.visibility_timeout = visibility_timeout
        self._shards: dict[tuple[str, int], _Shard] = {}
        self._inflight: dict[str, tuple[float, tuple[str, int], _Entry]] = {}
        self._seq = itertools.count()
        self._event = asyncio.Event()
        self.down = False
        # Recent messages, for tests and the dev harness; bounded so a long dev session does not grow.
        self.published: deque[QueueMessage] = deque(maxlen=10_000)

    def _shard(self, topic: str, shard: int) -> _Shard:
        return self._shards.setdefault((topic, shard), _Shard())

    async def publish(self, message: QueueMessage) -> None:
        if self.down:
            raise ServiceUnavailable("queue unavailable")
        if not message.enqueued_at:
            message = message.model_copy(update={"enqueued_at": time.time()})
        shard = shard_for(message.company_id, self.shard_count)
        self._shard(message.topic, shard).push(_Entry(message, time.monotonic()))
        self.published.append(message)
        self._event.set()

    def _reclaim(self, now: float) -> None:
        for ack_id in [a for a, (deadline, _, _) in self._inflight.items() if deadline <= now]:
            _, key, entry = self._inflight.pop(ack_id)
            self._shard(*key).push(entry)

    async def pull(self, topic: str, shards: Sequence[int], max_messages: int) -> list[Delivery]:
        if self.down:
            raise ServiceUnavailable("queue unavailable")
        now = time.monotonic()
        self._reclaim(now)
        out: list[Delivery] = []
        active = [s for s in shards if (topic, s) in self._shards]
        for s in active:
            self._shards[(topic, s)].promote(now)
        progress = True
        while len(out) < max_messages and progress:
            progress = False
            for s in active:  # interleave shards
                entry = self._shards[(topic, s)].pop()
                if entry is None:
                    continue
                ack_id = uuid.uuid4().hex
                self._inflight[ack_id] = (now + self.visibility_timeout, (topic, s), entry)
                out.append(Delivery(message=entry.message, shard=s, ack_id=ack_id))
                progress = True
                if len(out) >= max_messages:
                    break
        return out

    async def ack(self, delivery: Delivery) -> None:
        self._inflight.pop(delivery.ack_id, None)

    async def nack(self, delivery: Delivery, delay_seconds: float, *, count_attempt: bool = True) -> None:
        item = self._inflight.pop(delivery.ack_id, None)
        if item is None:
            return
        _, key, entry = item
        if count_attempt:
            entry = _Entry(
                entry.message.model_copy(update={"attempt": entry.message.attempt + 1}), entry.first_enqueued
            )
        shard = self._shard(*key)
        if delay_seconds <= 0:
            shard.push(entry)
        else:
            heapq.heappush(shard.delayed, (time.monotonic() + delay_seconds, next(self._seq), entry))
        self._event.set()

    async def stats(self, topic: str) -> QueueStats:
        depth = 0
        oldest: float | None = None
        for (t, _), shard in self._shards.items():
            if t != topic:
                continue
            d, o = shard.depth_and_oldest()
            depth += d
            if o is not None:
                oldest = o if oldest is None else min(oldest, o)
        return QueueStats(depth=depth, oldest_age_seconds=(time.monotonic() - oldest) if oldest else 0.0)

    async def wait_for_messages(self, max_wait: float) -> None:
        self._event.clear()
        try:
            await asyncio.wait_for(self._event.wait(), max_wait)
        except TimeoutError:
            pass

    def pending(self, topic: str | None = None) -> int:
        total = 0
        for (t, _), shard in self._shards.items():
            if topic is None or t == topic:
                total += shard.depth_and_oldest()[0]
        return total + len(self._inflight)

    async def ping(self) -> bool:
        return not self.down

    async def close(self) -> None:
        return None
