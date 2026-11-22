"""sms_logging handlers.

* ``on_event`` (events topic): insert-once ``SmsActivity`` (skipped if the first status seen is
  failed), then queue ``sync.sms`` carrying the still-encrypted text.
* ``sync_sms`` (sync topic): decrypt transiently, log to the connector through ``SmsLogger`` with
  ``find_by_reference`` idempotency, mark synced. The text is never written to storage.
"""

from __future__ import annotations

import logging
from typing import cast

from framework_sanic.core.container import Services
from framework_sanic.core.domain.records import Lane, Record
from framework_sanic.core.features.guards import SyncGuards
from framework_sanic.core.features.jobs import ACK, JobResult, deadletter, retry
from framework_sanic.core.intake import InboundEvent, decrypt_sms_text
from framework_sanic.core.ports.queue import TOPIC_SYNC, QueueMessage

from .merge import sms_merge_fn
from .models import SmsActivity, SmsLog
from .port import SmsLoggingConnector

log = logging.getLogger(__name__)


class SmsLogging:
    def __init__(self, services: Services) -> None:
        self.s = services
        self.connector = cast(SmsLoggingConnector, services.connector)  # checked in feature setup
        self.guards = SyncGuards(services)

    async def on_event(self, event: InboundEvent) -> JobResult:
        ev = event.sms
        assert ev is not None
        company_id = event.company_id
        tgt = ev.target
        if tgt and not await self.s.targets.is_logging_enabled(company_id, tgt.type, tgt.id):
            return ACK
        key = (company_id, ev.id)
        now = self.s.clock.now()
        result = await self.s.repo.upsert(
            SmsActivity.KIND,
            key,
            sms_merge_fn(
                ev, company_id=company_id, ttl_seconds=self.s.settings.activity_ttl_seconds, now=now
            ),
        )
        if result == "ignored":
            return ACK
        rec = await self.s.repo.get(SmsActivity.KIND, key)
        if rec is None or SmsActivity.from_record(rec).synced:
            return ACK
        # The encrypted text travels only inside queue messages; it is never written to storage.
        await self.s.queue.publish(
            QueueMessage(
                message_id=f"sync.sms:{company_id}:{ev.id}",
                topic=TOPIC_SYNC,
                company_id=company_id,
                lane=Lane.LIVE,
                job="sync.sms",
                payload={"sms_id": ev.id, "text_enc": event.text_enc},
                enqueued_at=now,
            )
        )
        return ACK

    async def sync_sms(self, msg: QueueMessage) -> JobResult:
        sms_id = int(msg.payload["sms_id"])
        key = (msg.company_id, sms_id)
        rec = await self.s.repo.get(SmsActivity.KIND, key)
        if rec is None:
            return ACK
        act = SmsActivity.from_record(rec)
        if act.synced:
            return ACK
        cred = await self.guards.check(msg, act.owner_user_id)
        if isinstance(cred, JobResult):
            return cred
        text: str | None = None
        if msg.payload.get("text_enc"):
            try:
                text = await decrypt_sms_text(self.s.cipher, msg.company_id, sms_id, msg.payload["text_enc"])
            except Exception:  # noqa: BLE001
                return deadletter("cannot decrypt SMS text")
        if not await self.guards.lease(act.activity_key):
            return retry(2.0, "sync in progress", count_attempt=False)
        try:
            external_number = (
                act.from_number
                if act.direction == "inbound"
                else (act.to_numbers[0] if act.to_numbers else None)
            )
            contact = act.external_contact_ref or await self.s.contacts.resolve_or_create(
                msg.company_id,
                cred,
                act.dialpad_contact_ref,
                external_number,
                create_if_unknown=self.s.settings.UNKNOWN_CALLER_POLICY == "create_contact",
            )
            ref = act.external_ref or await self.connector.find_by_reference(cred, act.activity_key)
            if ref is None:
                payload = act.model_copy(update={"external_contact_ref": contact})
                ref = await self.connector.log_sms(cred, SmsLog(activity=payload, text=text))
            final_ref = ref

            def merge(cur: Record | None) -> Record | None:
                if cur is None:
                    return None
                return {
                    **cur,
                    "synced": True,
                    "external_ref": final_ref.model_dump(mode="json"),
                    "external_contact_ref": contact.model_dump(mode="json") if contact else None,
                }

            await self.s.repo.upsert(SmsActivity.KIND, key, merge)
            return ACK  # the decrypted text goes out of scope here and is never stored
        except Exception as exc:  # noqa: BLE001
            return await self.guards.on_error(exc, msg, cred)
        finally:
            await self.guards.release(act.activity_key)
