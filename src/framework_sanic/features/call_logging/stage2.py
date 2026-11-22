"""call_logging stage 2: sync call activities and notes to the external service (rate limited, idempotent).

Parking, credentials, rate limits, leases and error mapping come from the framework's
``SyncGuards``; this module owns only what is written and when.

* Parked companies (invalid Dialpad connection, or no usable external credential) are deferred
  without counting attempts; stage 1 keeps flowing and messages resume after reconnect.
* Per-connector, per-company token bucket; 429s honour Retry-After with jitter.
* Idempotency: the logged record is an ``EntityMap`` keyed by the activity key, and
  ``find_by_reference`` (the reference stored on the CRM record) runs before any create, so duplicate deliveries never create a second external record. A short
  lease is a fast path that also covers concurrent duplicate deliveries.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from framework_sanic.core.container import Services
from framework_sanic.core.domain.entities import EntityMap
from framework_sanic.core.domain.records import Record
from framework_sanic.core.features.guards import SyncGuards
from framework_sanic.core.features.jobs import ACK, JobResult, deadletter, retry
from framework_sanic.core.ports.queue import QueueMessage

from .merge import sync_view_hash
from .models import CallActivity, Note
from .policy import CallLoggingSettings, CallLogPolicy
from .port import CallLoggingConnector

log = logging.getLogger(__name__)

NOTE_WAIT_SECONDS = 30.0
MAX_NOTE_ATTEMPTS = 60


class Stage2:
    def __init__(self, services: Services, policy: CallLogPolicy | None = None) -> None:
        self.s = services
        self.connector = cast(CallLoggingConnector, services.connector)  # checked in feature setup
        self.guards = SyncGuards(services)
        self.policy = policy or CallLogPolicy(CallLoggingSettings(), services.connector)

    # --- calls ------------------------------------------------------------------------------
    async def sync_call(self, msg: QueueMessage) -> JobResult:
        key = (msg.company_id, str(msg.payload["call_id"]))
        rec = await self.s.repo.get(CallActivity.KIND, key)
        if rec is None:
            return ACK
        act = CallActivity.from_record(rec)
        if not act.terminal:
            return ACK
        view = sync_view_hash(act)
        pending = sorted(set(act.recording_urls) - set(act.synced_recordings))
        logged = await self.s.entity_maps.get(msg.company_id, act.activity_key)
        if logged and act.synced_hash == view and not pending:
            return ACK
        if logged is None and not self.policy.should_log(act):
            if act.synced_hash != view:  # remember the decision for this version of the call
                await self._save_call_sync(key, None, view, [])
            return ACK

        cred = await self.guards.check(msg, act.owner_user_id)
        if isinstance(cred, JobResult):
            return cred
        lease = act.activity_key
        if not await self.guards.lease(lease):
            return retry(2.0, "sync in progress", count_attempt=False)
        try:
            contact = act.contact
            if contact is None:
                contact = await self.s.contacts.resolve_or_create(
                    msg.company_id,
                    cred,
                    act.dialpad_contact_ref,
                    act.external_number,
                    create_if_unknown=self.s.settings.UNKNOWN_CALLER_POLICY == "create_contact",
                )
                if contact is None and self.s.settings.UNKNOWN_CALLER_POLICY == "skip":
                    await self._save_call_sync(key, None, view, pending)
                    return ACK
            logged = logged or await self.connector.find_by_reference(cred, act.activity_key)
            if logged is None or act.synced_hash != view:
                to_send = act.model_copy(update={"contact": contact})
                logged = await self.connector.upsert_call(cred, to_send, logged)
            logged = await self.s.entity_maps.save(msg.company_id, logged, dialpad_id=act.activity_key)
            if "recordings" in self.connector.capabilities and self.policy.settings.recordings:
                for url in pending:
                    await self.connector.attach_recording(cred, logged, url)
            await self._save_call_sync(key, contact, view, pending)
            return ACK
        except Exception as exc:  # noqa: BLE001 - mapped by the connector
            return await self.guards.on_error(exc, msg, cred)
        finally:
            await self.guards.release(lease)

    async def _save_call_sync(
        self,
        key: tuple[str, str],
        contact: EntityMap | None,
        view: str,
        recordings: list[str],
    ) -> None:
        def merge(cur: Record | None) -> Record | None:
            if cur is None:
                return None
            act = CallActivity.from_record(cur)
            update: dict[str, Any] = {
                "synced_hash": view,
                "synced_recordings": sorted(set(act.synced_recordings) | set(recordings)),
            }
            if contact is not None and act.contact is None:
                update["contact"] = contact
            return act.model_copy(update=update).to_record()

        await self.s.repo.upsert(CallActivity.KIND, key, merge)

    # --- notes ------------------------------------------------------------------------------
    async def sync_note(self, msg: QueueMessage) -> JobResult:
        key = (msg.company_id, str(msg.payload["note_id"]))
        rec = await self.s.repo.get(Note.KIND, key)
        if rec is None:
            return ACK
        note = Note.from_record(rec)
        if note.synced:
            return ACK
        act_rec = await self.s.repo.get(CallActivity.KIND, (msg.company_id, note.call_id))
        act = CallActivity.from_record(act_rec) if act_rec else None
        call = await self.s.entity_maps.get(msg.company_id, act.activity_key) if act else None
        if call is None:
            if msg.attempt >= MAX_NOTE_ATTEMPTS:
                return deadletter("call activity never synced")
            return retry(NOTE_WAIT_SECONDS, "waiting for call activity to sync")
        cred = await self.guards.check(msg, note.author_user_id)
        if isinstance(cred, JobResult):
            return cred
        lease = f"note:{msg.company_id}:{note.note_id}"
        if not await self.guards.lease(lease):
            return retry(2.0, "sync in progress", count_attempt=False)
        try:
            note_map = await self.connector.add_note(cred, call, note)
            await self.s.entity_maps.save(msg.company_id, note_map, dialpad_id=note.activity_key)
            await self.s.repo.upsert(Note.KIND, key, lambda cur: {**cur, "synced": True} if cur else None)
            return ACK
        except Exception as exc:  # noqa: BLE001
            return await self.guards.on_error(exc, msg, cred)
        finally:
            await self.guards.release(lease)
