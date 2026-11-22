"""``hubspot.notes``: a note written in the contact card becomes a HubSpot note associated with the
logged call. Nothing is stored locally: the note travels in a queued job that waits for the call
to be logged, then creates the note once (``find_by_reference`` on the note's key)."""

from __future__ import annotations

from typing import cast

from framework_sanic.sdk import (
    ACK,
    TOPIC_SYNC,
    FeatureContext,
    JobResult,
    Lane,
    NoteIn,
    NoteListener,
    NoteOut,
    Principal,
    QueueMessage,
    SyncGuards,
    deadletter,
    retry,
)

from ..activities import CallNote
from ..connector import HubSpotConnector

SYNC_JOB = "hubspot.sync_note"
WAIT_SECONDS = 30.0  # for the call to be logged
MAX_ATTEMPTS = 60


class HubSpotNotes(NoteListener):
    name = "hubspot.notes"

    def configure(self, ctx: FeatureContext) -> None:
        self.connector = cast(HubSpotConnector, self.services.connector)
        self.guards = SyncGuards(self.services)
        ctx.job(TOPIC_SYNC, SYNC_JOB)(self.sync_note)

    async def on_note(self, principal: Principal, note: NoteIn) -> NoteOut:
        now = self.services.clock.now()
        call_note = CallNote(
            company_id=principal.company_id,
            note_id=note.note_id,
            call_id=note.call_id,
            author_user_id=principal.user_id,
            body=note.body.strip(),
            created_at=now,
        )
        await self.services.queue.publish(
            QueueMessage(
                message_id=f"{SYNC_JOB}:{principal.company_id}:{note.note_id}",  # retries are idempotent
                topic=TOPIC_SYNC,
                company_id=principal.company_id,
                lane=Lane.UI,
                job=SYNC_JOB,
                payload=call_note.model_dump(mode="json"),
                enqueued_at=now,
            )
        )
        return NoteOut(note_id=note.note_id, call_id=note.call_id, synced=False)

    async def sync_note(self, msg: QueueMessage) -> JobResult:
        note = CallNote.model_validate(msg.payload)
        call = await self.services.entity_maps.get(msg.company_id, note.call_activity_key)
        if call is None:
            if msg.attempt >= MAX_ATTEMPTS:
                return deadletter("call was never logged")
            return retry(WAIT_SECONDS, "waiting for the call to be logged")
        cred = await self.guards.check(msg, note.author_user_id)
        if isinstance(cred, JobResult):
            return cred
        if not await self.guards.lease(note.activity_key):
            return retry(2.0, "sync in progress", count_attempt=False)
        try:
            note_map = await self.connector.add_note(cred, call, note)
            await self.services.entity_maps.save(msg.company_id, note_map, dialpad_id=note.activity_key)
            return ACK
        except Exception as exc:  # noqa: BLE001
            return await self.guards.on_error(exc, msg, cred)
        finally:
            await self.guards.release(note.activity_key)


feature = HubSpotNotes()
