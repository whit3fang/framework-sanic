"""call_logging stage 1: merge delivered call events into activity records. Cheap, never calls
external services.

The activity upsert and its outbox entry are written in the same UnitOfWork. After commit the
outbox entry is published immediately (fast path); the framework's ``OutboxRelay`` republishes
anything left behind.
"""

from __future__ import annotations

import logging

from framework_sanic.core.container import Services
from framework_sanic.core.domain.entities import OutboxEntry
from framework_sanic.core.domain.records import Lane, stable_hash
from framework_sanic.core.features.jobs import ACK, JobResult
from framework_sanic.core.intake import InboundEvent
from framework_sanic.core.ports.cache import CacheUnavailable
from framework_sanic.core.ports.queue import TOPIC_SYNC
from framework_sanic.core.ports.repository import UnitOfWork, run_in_uow
from framework_sanic.core.services.outbox import publish_and_clear

from .merge import call_merge_fn, sync_view_hash
from .models import CallActivity

log = logging.getLogger(__name__)

DEDUPE_TTL_SECONDS = 86400.0


class Stage1:
    def __init__(self, services: Services) -> None:
        self.s = services

    async def _seen(self, key: str) -> bool:
        try:
            return await self.s.cache.get(key) is not None
        except CacheUnavailable:
            return False

    async def _mark_seen(self, key: str) -> None:
        try:
            await self.s.cache.set(key, b"1", DEDUPE_TTL_SECONDS)
        except CacheUnavailable:
            pass

    async def on_call(self, event: InboundEvent) -> JobResult:
        ev = event.call
        assert ev is not None
        company_id = event.company_id
        dedupe = f"dedupe:{company_id}:{stable_hash(ev.call_id, ev.state, ev.event_timestamp)}"
        if await self._seen(dedupe):  # fast path only; correctness comes from the atomic merge
            return ACK

        targets = self.s.targets
        if ev.is_operator_leg:
            # Department/call-center calls: the department leg is logged. When the group target's
            # logging is enabled, drop the operator-leg events. (VERIFY entry_point_target semantics)
            ept = ev.entry_point_target
            assert ept is not None
            if await targets.is_logging_enabled(company_id, ept.type, ept.id):
                await self._mark_seen(dedupe)
                return ACK
        tgt = ev.target
        if tgt and not await targets.is_logging_enabled(company_id, tgt.type, tgt.id):
            await self._mark_seen(dedupe)
            return ACK

        key = (company_id, ev.call_id)
        merge = call_merge_fn(ev, company_id=company_id, ttl_seconds=self.s.settings.activity_ttl_seconds)
        now = self.s.clock.now()

        async def work(uow: UnitOfWork) -> OutboxEntry | None:
            result = await uow.upsert(CallActivity.KIND, key, merge)
            if result not in ("created", "updated"):
                return None
            rec = await uow.get(CallActivity.KIND, key)
            if rec is None:
                return None
            act = CallActivity.from_record(rec)
            if not act.terminal:
                return None  # only terminal (and later additive) states are synced externally
            entry = OutboxEntry(
                company_id=company_id,
                outbox_id=f"sync.call:{company_id}:{ev.call_id}:{sync_view_hash(act)[:16]}",
                topic=TOPIC_SYNC,
                lane=Lane.LIVE.value,
                job="sync.call",
                payload={"call_id": ev.call_id},
                created_at=now,
            )
            await uow.add_outbox(entry.key(), entry.to_record())
            return entry

        entry = await run_in_uow(self.s.repo, work)
        if entry is not None:
            await publish_and_clear(self.s.repo, self.s.queue, entry)
        await self._mark_seen(dedupe)
        return ACK
