"""Call logging (used by ``listeners/calls.py`` and ``listeners/notes.py``): one HubSpot call per Dialpad call, its recording link
and its notes, each associated with the matched contact."""

from __future__ import annotations

from framework_sanic.sdk import ConnectorCredential, EntityMap

from .activities import CallNote, LoggedCall
from .crm import CrmClient
from .mapping import call_properties, note_properties, recording_properties
from .references import ActivityReferences, logged


class CallActivities:
    def __init__(self, crm: CrmClient, references: ActivityReferences) -> None:
        self.crm = crm
        self.references = references

    async def upsert(
        self, cred: ConnectorCredential, activity: LoggedCall, existing: EntityMap | None
    ) -> EntityMap:
        """Create the call, or update ``existing`` (the HubSpot call already logged for it)."""
        await self.references.ensure_property(cred, "calls")
        props = call_properties(activity)
        if existing:
            await self.crm.update(cred, "calls", existing.external_id, props)
            call = existing
        else:
            created = await self.crm.create(cred, "calls", props)
            call = logged(activity.activity_key, "calls", created["id"])
        if activity.contact:
            await self.crm.associate(
                cred, "calls", call.external_id, "contacts", activity.contact.external_id
            )
        return call

    async def attach_recording(self, cred: ConnectorCredential, call: EntityMap, url: str) -> None:
        await self.crm.update(cred, "calls", call.external_id, recording_properties(url))

    async def add_note(self, cred: ConnectorCredential, call: EntityMap, note: CallNote) -> EntityMap:
        """Idempotent per note id: an existing note is only (re-)associated."""
        await self.references.ensure_property(cred, "notes")
        existing = await self.references.find(cred, note.activity_key)
        if existing is None:
            created = await self.crm.create(cred, "notes", note_properties(note))
            existing = logged(note.activity_key, "notes", created["id"])
        await self.crm.associate(cred, "notes", existing.external_id, call.external_type, call.external_id)
        return existing
