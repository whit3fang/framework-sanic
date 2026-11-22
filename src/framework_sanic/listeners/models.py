"""Activity models: Dialpad calls and SMS as plain records, built from one event each.

They carry no processing state; an integration that stores them and needs more (sync
bookkeeping, a fold over several events) subclasses them.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from framework_sanic.core.domain.entities import Entity
from framework_sanic.core.domain.events import CallEvent, SmsEvent, contact_ref_from_party
from framework_sanic.core.domain.records import Key


class CallActivity(Entity):
    """One call leg (``call_id``) as Dialpad describes it."""

    KIND: ClassVar[str] = "CallActivity"
    call_id: str
    state: str | None = None
    terminal: bool = False  # hangup, missed or voicemail
    direction: str | None = None
    external_number: str | None = None
    internal_number: str | None = None
    dialpad_contact_ref: str | None = None  # "contact:<id>" or "phone:<number>" (EntityMap key)
    contact_name: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    entry_point_target_type: str | None = None
    entry_point_target_id: str | None = None
    is_department_leg: bool | None = None
    owner_user_id: str | None = None  # the Dialpad user who handled the call, when known
    started_at: int | None = None
    rang_at: int | None = None
    connected_at: int | None = None
    ended_at: int | None = None
    duration_ms: float | None = None
    voicemail_url: str | None = None
    recording_urls: list[str] = Field(default_factory=list)
    expires_at: float | None = None  # set it to have the platform delete the record afterwards

    def key(self) -> Key:
        return (self.company_id, self.call_id)

    @property
    def activity_key(self) -> str:
        """Stable id of this call: the ``EntityMap`` dialpad id of whatever is logged for it."""
        return f"dialpad:{self.company_id}:call:{self.call_id}"

    @classmethod
    def from_event(cls, ev: CallEvent, *, company_id: str) -> CallActivity:
        """The call as this one event describes it (no merging with earlier events)."""
        if ev.is_department_leg:
            owner = ev.operator_user_id()
        elif ev.target and ev.target.type == "user":
            owner = ev.target.id
        else:
            owner = None
        return cls(
            company_id=company_id,
            call_id=ev.call_id,
            state=ev.state,
            terminal=ev.is_terminal,
            direction=ev.direction,
            external_number=ev.external_number,
            internal_number=ev.internal_number,
            dialpad_contact_ref=contact_ref_from_party(ev.contact, ev.external_number),
            contact_name=ev.contact.name if ev.contact else None,
            target_type=ev.target.type if ev.target else None,
            target_id=ev.target.id if ev.target else None,
            entry_point_target_type=ev.entry_point_target.type if ev.entry_point_target else None,
            entry_point_target_id=ev.entry_point_target.id if ev.entry_point_target else None,
            is_department_leg=ev.is_department_leg if ev.target else None,
            owner_user_id=owner,
            started_at=ev.date_started,
            rang_at=ev.date_rang,
            connected_at=ev.date_connected,
            ended_at=ev.date_ended,
            duration_ms=ev.duration,
            voicemail_url=ev.voicemail_link,
            recording_urls=sorted(set(ev.recording_url)),
        )


class SmsActivity(Entity):
    """One SMS as Dialpad describes it. The message text is never part of it."""

    KIND: ClassVar[str] = "SmsActivity"
    sms_id: int
    direction: str | None = None
    from_number: str | None = None
    to_numbers: list[str] = Field(default_factory=list)
    dialpad_contact_ref: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    owner_user_id: str | None = None
    created_date: int | None = None
    message_status: str | None = None
    expires_at: float | None = None

    def key(self) -> Key:
        return (self.company_id, self.sms_id)

    @property
    def activity_key(self) -> str:
        return f"dialpad:{self.company_id}:sms:{self.sms_id}"

    @property
    def external_number(self) -> str | None:
        """The other party's number."""
        return (
            self.from_number
            if self.direction == "inbound"
            else (self.to_numbers[0] if self.to_numbers else None)
        )

    @classmethod
    def from_event(cls, ev: SmsEvent, *, company_id: str) -> SmsActivity:
        external = (
            ev.from_number if ev.direction == "inbound" else (ev.to_number[0] if ev.to_number else None)
        )
        return cls(
            company_id=company_id,
            sms_id=ev.id,
            direction=ev.direction,
            from_number=ev.from_number,
            to_numbers=list(ev.to_number),
            dialpad_contact_ref=contact_ref_from_party(ev.contact, external),
            target_type=ev.target.type if ev.target else None,
            target_id=ev.target.id if ev.target else None,
            owner_user_id=ev.target.id if ev.target and ev.target.type == "user" else None,
            created_date=ev.created_date or ev.event_timestamp,
            message_status=ev.message_status,
        )


class NoteIn(BaseModel):
    """A note a user wrote in the contact card about a call (``POST /notes``). Not stored by the
    framework: it goes to the integration's ``NoteListener``."""

    note_id: str = Field(
        pattern=r"^[A-Za-z0-9_-]{8,64}$",
        description="Client-generated; retries with the same id are idempotent",
    )
    call_id: str = Field(min_length=1, max_length=64)
    body: str = Field(min_length=1, max_length=10_000)
    dialpad_ref: str | None = Field(default=None, max_length=200)

    def activity_key(self, company_id: str) -> str:
        """Stable id of this note (e.g. its ``EntityMap`` dialpad id)."""
        return f"dialpad:{company_id}:note:{self.note_id}"

    def call_activity_key(self, company_id: str) -> str:
        return f"dialpad:{company_id}:call:{self.call_id}"


class NoteOut(BaseModel):
    note_id: str
    call_id: str
    synced: bool = False  # True when the note already reached the external system
