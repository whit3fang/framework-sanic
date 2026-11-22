"""call_logging data model. Owned by this feature, not by the framework core."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import Field

from framework_sanic.core.domain.entities import Entity, EntityMap
from framework_sanic.core.domain.records import Key


class CallActivity(Entity):
    """Folded state of one call leg (``call_id``)."""

    KIND: ClassVar[str] = "CallActivity"
    call_id: str
    state: str | None = None
    terminal: bool = False
    direction: str | None = None
    external_number: str | None = None
    internal_number: str | None = None
    dialpad_contact_ref: str | None = None
    contact_name: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    entry_point_target_type: str | None = None
    entry_point_target_id: str | None = None
    is_department_leg: bool | None = None
    owner_user_id: str | None = None
    started_at: int | None = None
    rang_at: int | None = None
    connected_at: int | None = None
    ended_at: int | None = None
    duration_ms: float | None = None
    voicemail_url: str | None = None
    recording_urls: list[str] = Field(default_factory=list)  # additive; linked, never copied
    # For each scalar field: the rank of the event that set it. Makes the fold order-independent.
    field_ranks: dict[str, list[Any]] = Field(default_factory=dict)
    expires_at: float | None = None
    # Stage-2 sync bookkeeping (never touched by the fold). The logged record's own map is an
    # ``EntityMap`` row keyed by ``activity_key``; ``contact`` is a copy of the contact map used
    # when the call was first logged (a later rematch does not change it).
    contact: EntityMap | None = None
    synced_hash: str | None = None
    synced_recordings: list[str] = Field(default_factory=list)

    def key(self) -> Key:
        return (self.company_id, self.call_id)

    @property
    def activity_key(self) -> str:
        return f"dialpad:{self.company_id}:call:{self.call_id}"


class Note(Entity):
    KIND: ClassVar[str] = "Note"
    note_id: str  # client-generated, so retries are idempotent
    call_id: str
    dialpad_contact_ref: str | None = None
    author_user_id: str
    body: str
    created_at: float
    synced: bool = False

    def key(self) -> Key:
        return (self.company_id, self.note_id)

    @property
    def activity_key(self) -> str:
        """Dialpad id of the note's ``EntityMap`` (and the reference stored on the CRM note)."""
        return f"dialpad:{self.company_id}:note:{self.note_id}"


ENTITIES: tuple[type[Entity], ...] = (CallActivity, Note)
