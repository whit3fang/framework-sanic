"""``hubspot.sms``: every Dialpad SMS becomes one HubSpot communication.

1. ``on_sms`` (events topic): store the SMS metadata once, then queue ``hubspot.sync_sms``
   carrying the still-encrypted text. The text is never written to storage.
2. ``sync_sms`` (sync topic): decrypt transiently, match the contact, log it
   (``find_by_reference`` first), mark it synced.
"""

from __future__ import annotations

from typing import cast

from framework_sanic.sdk import (
    ACK,
    TOPIC_SYNC,
    FeatureContext,
    InboundEvent,
    JobResult,
    Lane,
    QueueMessage,
    Record,
    SmsListener,
    SyncGuards,
    deadletter,
    decrypt_sms_text,
    retry,
)

from ..activities import LoggedSms
from ..connector import HubSpotConnector
from .settings import SmsLogSettings

SYNC_JOB = "hubspot.sync_sms"


class HubSpotSms(SmsListener):
    name = "hubspot.sms"

    def configure(self, ctx: FeatureContext) -> None:
        self.settings = ctx.settings(SmsLogSettings)
        self.connector = cast(HubSpotConnector, self.services.connector)
        self.guards = SyncGuards(self.services)
        ctx.job(TOPIC_SYNC, SYNC_JOB)(self.sync_sms)
        ctx.declare_company_data(LoggedSms)

    async def on_sms(self, event: InboundEvent) -> JobResult:
        ev = event.sms
        assert ev is not None
        s = self.services
        company_id = event.company_id
        if not self.settings.direction_allowed(ev.direction):
            return ACK
        tgt = ev.target
        if tgt and not await s.targets.is_logging_enabled(company_id, tgt.type, tgt.id):
            return ACK
        key = (company_id, ev.id)
        now = s.clock.now()

        def insert_once(cur: Record | None) -> Record | None:
            if cur is not None:
                return cur
            if (ev.message_status or "").lower() == "failed":  # first status seen is failed: skip
                return None
            sms = LoggedSms.from_event(ev, company_id=company_id)
            base = (sms.created_date / 1000.0) if sms.created_date else now
            return sms.model_copy(
                update={"expires_at": base + self.settings.retention_days * 86400.0}
            ).to_record()

        await s.repo.upsert(LoggedSms.KIND, key, insert_once)
        rec = await s.repo.get(LoggedSms.KIND, key)
        if rec is None or LoggedSms.from_record(rec).synced:
            return ACK
        # The encrypted text travels only inside queue messages; it is never written to storage.
        await s.queue.publish(
            QueueMessage(
                message_id=f"{SYNC_JOB}:{company_id}:{ev.id}",
                topic=TOPIC_SYNC,
                company_id=company_id,
                lane=Lane.LIVE,
                job=SYNC_JOB,
                payload={"sms_id": ev.id, "text_enc": event.text_enc},
                enqueued_at=now,
            )
        )
        return ACK

    async def sync_sms(self, msg: QueueMessage) -> JobResult:
        s = self.services
        sms_id = int(msg.payload["sms_id"])
        key = (msg.company_id, sms_id)
        rec = await s.repo.get(LoggedSms.KIND, key)
        if rec is None:
            return ACK
        act = LoggedSms.from_record(rec)
        if act.synced:
            return ACK
        cred = await self.guards.check(msg, act.owner_user_id)
        if isinstance(cred, JobResult):
            return cred
        text: str | None = None
        if msg.payload.get("text_enc") and self.settings.include_text:
            try:
                text = await decrypt_sms_text(s.cipher, msg.company_id, sms_id, msg.payload["text_enc"])
            except Exception:  # noqa: BLE001
                return deadletter("cannot decrypt SMS text")
        lease = await self.guards.lease(act.activity_key)
        if lease is None:
            return retry(2.0, "sync in progress", count_attempt=False)
        try:
            contact = act.contact or await s.contacts.resolve_or_create(
                msg.company_id,
                cred,
                act.dialpad_contact_ref,
                act.external_number,
                create_if_unknown=self.settings.unknown_caller == "create_contact",
            )
            if contact is None and self.settings.unknown_caller == "skip":
                await s.repo.upsert(LoggedSms.KIND, key, lambda cur: {**cur, "synced": True} if cur else None)
                return ACK
            logged = await s.entity_maps.get(msg.company_id, act.activity_key)
            logged = logged or await self.connector.find_by_reference(cred, act.activity_key)
            if logged is None:
                logged = await self.connector.log_sms(cred, act.model_copy(update={"contact": contact}), text)
            await s.entity_maps.save(msg.company_id, logged, dialpad_id=act.activity_key)
            contact_json = contact.model_dump(mode="json") if contact else None
            await s.repo.upsert(
                LoggedSms.KIND,
                key,
                lambda cur: {**cur, "synced": True, "contact": contact_json} if cur else None,
            )
            return ACK  # the decrypted text goes out of scope here and is never stored
        except Exception as exc:  # noqa: BLE001
            return await self.guards.on_error(exc, msg, cred)
        finally:
            await self.guards.release(act.activity_key, lease)


feature = HubSpotSms()
