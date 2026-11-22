"""``hubspot.calls``: every Dialpad call becomes one HubSpot call.

1. ``on_call`` (events topic, cheap, never calls HubSpot): fold the event into the stored
   ``LoggedCall`` and, when the call is terminal, queue ``hubspot.sync_call``. The fold and the
   queued job are written in one unit of work (transactional outbox), so a crash cannot lose it.
2. ``sync_call`` (sync topic): match the contact, create or update the HubSpot call
   (``find_by_reference`` first, so retries never duplicate), attach recordings.

Parking, credentials, rate limits, leases and error mapping come from the framework's
``SyncGuards``.
"""

from __future__ import annotations

from typing import Any, ClassVar, cast

from framework_sanic.sdk import (
    ACK,
    TOPIC_SYNC,
    CallListener,
    EntityMap,
    FeatureContext,
    InboundEvent,
    JobResult,
    Lane,
    OutboxEntry,
    QueueMessage,
    Record,
    SyncGuards,
    UnitOfWork,
    publish_and_clear,
    retry,
    run_in_uow,
)

from ..activities import LoggedCall
from ..connector import HubSpotConnector
from .fold import call_merge_fn, sync_view_hash
from .settings import CallLogSettings

SYNC_JOB = "hubspot.sync_call"


class HubSpotCalls(CallListener):
    name = "hubspot.calls"
    scopes: ClassVar[tuple[str, ...]] = ("calls:list", "recordings_export")

    def configure(self, ctx: FeatureContext) -> None:
        self.settings = ctx.settings(CallLogSettings)
        self.connector = cast(HubSpotConnector, self.services.connector)
        self.guards = SyncGuards(self.services)
        ctx.job(TOPIC_SYNC, SYNC_JOB)(self.sync_call)
        ctx.declare_company_data(LoggedCall)

    # --- 1. fold ----------------------------------------------------------------------------
    async def on_call(self, event: InboundEvent) -> JobResult:
        ev = event.call
        assert ev is not None
        company_id = event.company_id
        targets = self.services.targets
        if ev.is_operator_leg:
            # Department/call-center calls: the department leg is logged. When the group target's
            # logging is enabled, drop the operator-leg events. (VERIFY entry_point_target semantics)
            ept = ev.entry_point_target
            assert ept is not None
            if await targets.is_logging_enabled(company_id, ept.type, ept.id):
                return ACK
        tgt = ev.target
        if tgt and not await targets.is_logging_enabled(company_id, tgt.type, tgt.id):
            return ACK

        key = (company_id, ev.call_id)
        merge = call_merge_fn(ev, company_id=company_id, ttl_seconds=self.settings.retention_days * 86400.0)
        now = self.services.clock.now()

        async def work(uow: UnitOfWork) -> OutboxEntry | None:
            if await uow.upsert(LoggedCall.KIND, key, merge) not in ("created", "updated"):
                return None
            rec = await uow.get(LoggedCall.KIND, key)
            act = LoggedCall.from_record(rec) if rec else None
            if act is None or not act.terminal:
                return None  # only terminal (and later additive) states reach HubSpot
            entry = OutboxEntry(
                company_id=company_id,
                outbox_id=f"{SYNC_JOB}:{company_id}:{ev.call_id}:{sync_view_hash(act)[:16]}",
                topic=TOPIC_SYNC,
                lane=Lane.LIVE.value,
                job=SYNC_JOB,
                payload={"call_id": ev.call_id},
                created_at=now,
            )
            await uow.add_outbox(entry.key(), entry.to_record())
            return entry

        entry = await run_in_uow(self.services.repo, work)
        if entry is not None:
            await publish_and_clear(self.services.repo, self.services.queue, entry)
        return ACK

    # --- 2. sync ----------------------------------------------------------------------------
    async def sync_call(self, msg: QueueMessage) -> JobResult:
        s = self.services
        key = (msg.company_id, str(msg.payload["call_id"]))
        rec = await s.repo.get(LoggedCall.KIND, key)
        if rec is None:
            return ACK
        act = LoggedCall.from_record(rec)
        if not act.terminal:
            return ACK
        view = sync_view_hash(act)
        pending = sorted(set(act.recording_urls) - set(act.synced_recordings))
        logged = await s.entity_maps.get(msg.company_id, act.activity_key)
        if logged and act.synced_hash == view and not pending:
            return ACK
        if logged is None and not self.settings.should_log(act.direction, act.state, act.duration_ms):
            if act.synced_hash != view:  # remember the decision for this version of the call
                await self._save_sync(key, None, view, [])
            return ACK

        cred = await self.guards.check(msg, act.owner_user_id)
        if isinstance(cred, JobResult):
            return cred
        lease = await self.guards.lease(act.activity_key)
        if lease is None:
            return retry(2.0, "sync in progress", count_attempt=False)
        try:
            contact = act.contact
            if contact is None:
                contact = await s.contacts.resolve_or_create(
                    msg.company_id,
                    cred,
                    act.dialpad_contact_ref,
                    act.external_number,
                    create_if_unknown=self.settings.unknown_caller == "create_contact",
                )
                if contact is None and self.settings.unknown_caller == "skip":
                    await self._save_sync(key, None, view, pending)
                    return ACK
            logged = logged or await self.connector.find_by_reference(cred, act.activity_key)
            if logged is None or act.synced_hash != view:
                update: dict[str, Any] = {"contact": contact}
                if not self.settings.recordings:  # no recording or voicemail links at all
                    update.update(recording_urls=[], voicemail_url=None)
                logged = await self.connector.upsert_call(cred, act.model_copy(update=update), logged)
            logged = await s.entity_maps.save(msg.company_id, logged, dialpad_id=act.activity_key)
            if self.settings.recordings:
                for url in pending:
                    await self.connector.attach_recording(cred, logged, url)
            await self._save_sync(key, contact, view, pending)
            return ACK
        except Exception as exc:  # noqa: BLE001 - mapped by the connector
            return await self.guards.on_error(exc, msg, cred)
        finally:
            await self.guards.release(act.activity_key, lease)

    async def _save_sync(
        self, key: tuple[str, str], contact: EntityMap | None, view: str, recordings: list[str]
    ) -> None:
        def merge(cur: Record | None) -> Record | None:
            if cur is None:
                return None
            act = LoggedCall.from_record(cur)
            update: dict[str, Any] = {
                "synced_hash": view,
                "synced_recordings": sorted(set(act.synced_recordings) | set(recordings)),
            }
            if contact is not None and act.contact is None:
                update["contact"] = contact
            return act.model_copy(update=update).to_record()

        await self.services.repo.upsert(LoggedCall.KIND, key, merge)


feature = HubSpotCalls()
