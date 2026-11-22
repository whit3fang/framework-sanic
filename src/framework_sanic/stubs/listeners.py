"""Fake listeners (``FEATURES=fake.calls,fake.sms,fake.notes``) for tests, ``framework-sanic dev`` and
the docker-compose replay: they record what the framework delivered, nothing more.

Each delivery is folded into one record per call / SMS / note (states and recordings are set
unions, so duplicates and any order give the same record; ``deliveries`` counts every delivery
to show at-least-once behaviour). The SMS text is decrypted to prove it can be, never stored.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field

from framework_sanic.core.domain.entities import Entity
from framework_sanic.core.domain.records import Key, Record
from framework_sanic.core.features.context import FeatureContext
from framework_sanic.core.features.jobs import ACK, JobResult
from framework_sanic.core.intake import InboundEvent
from framework_sanic.core.services.session_service import Principal
from framework_sanic.listeners import CallActivity, CallListener, NoteIn, NoteListener, NoteOut, SmsListener


class ReceivedCall(Entity):
    KIND: ClassVar[str] = "FakeReceivedCall"
    call_id: str
    states: list[str] = Field(default_factory=list)
    terminal: bool = False
    recording_urls: list[str] = Field(default_factory=list)
    dialpad_contact_ref: str | None = None
    owner_user_id: str | None = None
    deliveries: int = 0

    def key(self) -> Key:
        return (self.company_id, self.call_id)


class ReceivedSms(Entity):
    KIND: ClassVar[str] = "FakeReceivedSms"
    sms_id: int
    direction: str | None = None
    text_decrypted: bool = False
    deliveries: int = 0

    def key(self) -> Key:
        return (self.company_id, self.sms_id)


class ReceivedNote(Entity):
    KIND: ClassVar[str] = "FakeReceivedNote"
    note_id: str
    call_id: str
    author_user_id: str
    body: str

    def key(self) -> Key:
        return (self.company_id, self.note_id)


class FakeCalls(CallListener):
    name = "fake.calls"

    def configure(self, ctx: FeatureContext) -> None:
        ctx.declare_company_data(ReceivedCall)

    async def on_call(self, event: InboundEvent) -> JobResult:
        assert event.call is not None
        call = CallActivity.from_event(event.call, company_id=event.company_id)

        def merge(cur: Record | None) -> Record | None:
            rec = (
                ReceivedCall.from_record(cur)
                if cur
                else ReceivedCall(company_id=call.company_id, call_id=call.call_id)
            )
            return rec.model_copy(
                update={
                    "states": sorted(set(rec.states) | ({call.state} if call.state else set())),
                    "terminal": rec.terminal or call.terminal,
                    "recording_urls": sorted(set(rec.recording_urls) | set(call.recording_urls)),
                    "dialpad_contact_ref": rec.dialpad_contact_ref or call.dialpad_contact_ref,
                    "owner_user_id": rec.owner_user_id or call.owner_user_id,
                    "deliveries": rec.deliveries + 1,
                }
            ).to_record()

        await self.services.repo.upsert(ReceivedCall.KIND, (call.company_id, call.call_id), merge)
        return ACK


class FakeSms(SmsListener):
    name = "fake.sms"

    def configure(self, ctx: FeatureContext) -> None:
        ctx.declare_company_data(ReceivedSms)

    async def on_sms(self, event: InboundEvent) -> JobResult:
        assert event.sms is not None
        decrypted = (await event.sms_text()) is not None  # used transiently, never stored
        sms_id, company_id = event.sms.id, event.company_id

        def merge(cur: Record | None) -> Record | None:
            rec = ReceivedSms.from_record(cur) if cur else ReceivedSms(company_id=company_id, sms_id=sms_id)
            update = {
                "direction": event.sms.direction if event.sms else None,
                "deliveries": rec.deliveries + 1,
            }
            return rec.model_copy(
                update={**update, "text_decrypted": rec.text_decrypted or decrypted}
            ).to_record()

        await self.services.repo.upsert(ReceivedSms.KIND, (company_id, sms_id), merge)
        return ACK


class FakeNotes(NoteListener):
    name = "fake.notes"

    def configure(self, ctx: FeatureContext) -> None:
        ctx.declare_company_data(ReceivedNote)

    async def on_note(self, principal: Principal, note: NoteIn) -> NoteOut:
        rec = ReceivedNote(
            company_id=principal.company_id,
            note_id=note.note_id,
            call_id=note.call_id,
            author_user_id=principal.user_id,
            body=note.body,
        )
        await self.services.repo.upsert(ReceivedNote.KIND, rec.key(), lambda cur: cur or rec.to_record())
        return NoteOut(note_id=note.note_id, call_id=note.call_id, synced=True)


calls = FakeCalls()
sms = FakeSms()
notes = FakeNotes()
