"""What HubSpot logging stores about Dialpad activity: the framework's activity models plus this
integration's sync bookkeeping, and the note handed to ``add_note``."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from framework_sanic.sdk import CallActivity, EntityMap, SmsActivity


class LoggedCall(CallActivity):
    """A call folded from all of its events (see ``listeners/fold.py``) and its sync state."""

    # Per scalar field: the rank of the event that set it, so the fold is order-independent.
    field_ranks: dict[str, list[Any]] = Field(default_factory=dict)
    # Sync bookkeeping (never touched by the fold). The HubSpot call itself is the EntityMap row
    # keyed by ``activity_key``; ``contact`` is the contact map used when the call was first
    # logged, so a later rematch does not change it.
    contact: EntityMap | None = None
    synced_hash: str | None = None
    synced_recordings: list[str] = Field(default_factory=list)


class LoggedSms(SmsActivity):
    synced: bool = False
    contact: EntityMap | None = None


class CallNote(BaseModel):
    """A note to add to a logged call (from the contact card)."""

    company_id: str
    note_id: str
    call_id: str
    author_user_id: str
    body: str
    created_at: float

    @property
    def activity_key(self) -> str:
        return f"dialpad:{self.company_id}:note:{self.note_id}"

    @property
    def call_activity_key(self) -> str:
        return f"dialpad:{self.company_id}:call:{self.call_id}"
