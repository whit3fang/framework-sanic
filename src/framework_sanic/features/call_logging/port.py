"""What a connector must implement for ``call_logging``.

Idempotency: connectors store the activity key as a reference on the external record;
``find_by_reference`` is used before creating, so retries never duplicate. Every method that
creates a record returns an ``EntityMap`` whose ``dialpad_id`` is the activity key; the framework
stores it.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from framework_sanic.core.domain.entities import EntityMap
from framework_sanic.core.ports.connector import Connector, ConnectorCredential

from .models import CallActivity, Note


@runtime_checkable
class CallLogger(Protocol):
    async def upsert_call(
        self, cred: ConnectorCredential, activity: CallActivity, existing: EntityMap | None
    ) -> EntityMap:
        """Create the call record, or update ``existing`` (the record already logged for this
        call). Associate it with ``activity.contact`` when set."""
        ...

    async def attach_recording(self, cred: ConnectorCredential, call: EntityMap, url: str) -> None: ...

    async def add_note(self, cred: ConnectorCredential, call: EntityMap, note: Note) -> EntityMap:
        """Add ``note`` to the logged ``call``; returns the note's map (``note.activity_key``)."""
        ...

    async def find_by_reference(self, cred: ConnectorCredential, activity_key: str) -> EntityMap | None: ...


class CallLoggingConnector(Connector, CallLogger, Protocol):
    """A connector that supports call_logging."""
