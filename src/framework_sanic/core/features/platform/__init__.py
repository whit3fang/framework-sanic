"""platform: the always-on framework feature.

Owns the *receiving* side's lifecycle only: Dialpad subscription reconciliation (on demand, weekly
staggered sweep, change-log watch), the outbox relay and physical deletion of expired records.
It handles no events itself; integration features do that via ``ctx.on_event``.
"""

from __future__ import annotations

import logging

from ...domain.records import Lane
from ...ports.queue import TOPIC_CONTROL, QueueMessage
from ...services.outbox import OutboxRelay
from ...services.subscription_reconciler import ChangeLogWatcher, Sweeper
from ..context import FeatureContext
from ..jobs import ACK, JobResult

log = logging.getLogger(__name__)


class PlatformFeature:
    name = "platform"

    def setup(self, ctx: FeatureContext) -> None:
        s = ctx.services
        relay = OutboxRelay(s.repo, s.queue, s.clock)
        sweeper = Sweeper(s.repo, s.queue, s.clock, s.settings.SWEEP_TICK_SECONDS)
        changelog = ChangeLogWatcher(
            s.changelog, s.repo, s.queue, s.clock, s.settings.changelog_lifecycle_actions
        )

        ctx.declare_config(
            "DIALPAD_CALL_STATES",
            "LOGGABLE_TARGET_TYPES",
            "SWEEP_TICK_SECONDS",
            "PURGE_INTERVAL_SECONDS",
            "PURGE_BATCH_SIZE",
            "PURGE_MAX_BATCHES",
        )
        ctx.declare_index("Target", "company_id", "effective_enabled")
        ctx.declare_index("Outbox", "created_at")

        async def reconcile(msg: QueueMessage) -> JobResult:
            await s.reconciler.reconcile(
                msg.company_id, lane=msg.lane if msg.lane != Lane.LIVE else Lane.BACKFILL
            )
            return ACK

        ctx.job(TOPIC_CONTROL, "reconcile")(reconcile)

        async def relay_outbox() -> None:
            await relay.relay_once()

        async def sweep() -> None:
            await sweeper.tick()

        async def purge_expired() -> None:
            total = 0
            for _ in range(s.settings.PURGE_MAX_BATCHES):
                removed = await s.repo.purge_expired(limit=s.settings.PURGE_BATCH_SIZE)
                total += removed
                if removed < s.settings.PURGE_BATCH_SIZE:
                    break
            if total:
                log.info("purged %d expired records", total)

        ctx.schedule("outbox_relay", 15.0, relay_outbox, jitter=5.0)
        ctx.schedule("purge_expired", s.settings.PURGE_INTERVAL_SECONDS, purge_expired, jitter=60.0)
        ctx.schedule("weekly_sweep", float(s.settings.SWEEP_TICK_SECONDS), sweep, jitter=30.0)
        ctx.schedule("change_log", 300.0, changelog.poll_all, jitter=60.0)


feature = PlatformFeature()
