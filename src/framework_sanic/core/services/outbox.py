"""Transactional outbox (framework service, usable by any integration).

Write an ``OutboxEntry`` in the same UnitOfWork as the state change that requires a follow-up job,
then call ``publish_and_clear`` after commit (fast path). ``OutboxRelay`` (scheduled by the
platform feature) republishes entries left behind by a crash between commit and publish, so the
follow-up job is never lost. Jobs driven by the outbox must be idempotent.
"""

from __future__ import annotations

from ..domain.entities import OutboxEntry
from ..domain.records import Lane
from ..ports.queue import Queue, QueueMessage
from ..ports.repository import Repository
from ..ports.runtime import Clock

OUTBOX_RELAY_AGE_SECONDS = 30.0


def outbox_to_message(entry: OutboxEntry) -> QueueMessage:
    return QueueMessage(
        message_id=entry.outbox_id,
        topic=entry.topic,
        company_id=entry.company_id,
        lane=Lane(entry.lane),
        job=entry.job,
        payload=entry.payload,
        enqueued_at=entry.created_at,
    )


async def publish_and_clear(repo: Repository, queue: Queue, entry: OutboxEntry) -> None:
    await queue.publish(outbox_to_message(entry))
    await repo.delete(OutboxEntry.KIND, entry.key())


class OutboxRelay:
    """Republishes outbox entries whose fast-path publish did not happen."""

    def __init__(self, repo: Repository, queue: Queue, clock: Clock) -> None:
        self.repo = repo
        self.queue = queue
        self.clock = clock

    async def relay_once(self, *, min_age_seconds: float = OUTBOX_RELAY_AGE_SECONDS) -> int:
        now = self.clock.now()
        pending: list[OutboxEntry] = []
        async for rec in self.repo.query(OutboxEntry.KIND):
            entry = OutboxEntry.from_record(rec)
            if now - entry.created_at >= min_age_seconds:
                pending.append(entry)
        for entry in pending:
            await publish_and_clear(self.repo, self.queue, entry)
        return len(pending)
