"""call_logging notes: appended to a logged call. Client-generated ids make creation idempotent;
sync to the external service is async (``sync.note``)."""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from framework_sanic.core.domain.errors import InvalidInput
from framework_sanic.core.domain.records import Lane, Record
from framework_sanic.core.ports.queue import TOPIC_SYNC, Queue, QueueMessage
from framework_sanic.core.ports.repository import Repository
from framework_sanic.core.ports.runtime import Clock
from framework_sanic.core.services.session_service import Principal
from framework_sanic.http.routes import RequestContext, route

from .models import Note

_NOTE_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
MAX_NOTE_LENGTH = 10_000


class NotesService:
    def __init__(self, repo: Repository, queue: Queue, clock: Clock) -> None:
        self.repo = repo
        self.queue = queue
        self.clock = clock

    async def add(
        self, principal: Principal, *, note_id: str, call_id: str, body: str, dialpad_ref: str | None = None
    ) -> tuple[Note, bool]:
        if not _NOTE_ID.match(note_id):
            raise InvalidInput("note_id must be 8-64 characters of [A-Za-z0-9_-]")
        body = body.strip()
        if not body or len(body) > MAX_NOTE_LENGTH:
            raise InvalidInput(f"note body must be 1-{MAX_NOTE_LENGTH} characters")
        now = self.clock.now()

        def merge(cur: Record | None) -> Record | None:
            if cur is not None:
                return cur  # idempotent: a retry with the same note_id never creates a second note
            return Note(
                company_id=principal.company_id,
                note_id=note_id,
                call_id=call_id,
                dialpad_contact_ref=dialpad_ref,
                author_user_id=principal.user_id,
                body=body,
                created_at=now,
            ).to_record()

        result = await self.repo.upsert(Note.KIND, (principal.company_id, note_id), merge)
        rec = await self.repo.get(Note.KIND, (principal.company_id, note_id))
        assert rec is not None
        note = Note.from_record(rec)
        if note.author_user_id != principal.user_id:
            raise InvalidInput("note_id already used")
        if not note.synced:
            await self.queue.publish(
                QueueMessage(
                    message_id=f"note:{principal.company_id}:{note_id}",
                    topic=TOPIC_SYNC,
                    company_id=principal.company_id,
                    lane=Lane.UI,
                    job="sync.note",
                    payload={"note_id": note_id},
                    enqueued_at=now,
                )
            )
        return note, result == "created"


class NoteIn(BaseModel):
    note_id: str = Field(description="Client-generated id; retries with the same id are idempotent")
    call_id: str = Field(min_length=1, max_length=64)
    body: str = Field(min_length=1, max_length=10_000)
    dialpad_ref: str | None = Field(default=None, max_length=200)


class NoteOut(BaseModel):
    note_id: str
    call_id: str
    created: bool
    synced: bool


@route("POST", "/notes", auth="session", status=202)
async def add_note(req: NoteIn, ctx: RequestContext) -> NoteOut:
    """Appended to the call activity linked to the contact; synced asynchronously."""
    s = ctx.services
    note, created = await NotesService(s.repo, s.queue, s.clock).add(
        ctx.require_principal(),
        note_id=req.note_id,
        call_id=req.call_id,
        body=req.body,
        dialpad_ref=req.dialpad_ref,
    )
    return NoteOut(note_id=note.note_id, call_id=note.call_id, created=created, synced=note.synced)
