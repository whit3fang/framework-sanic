"""What a connector must implement for ``call_logging``.

Idempotency: connectors store the activity key as an external reference on the external record;
``find_by_reference`` is used before creating, so retries never duplicate.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from framework_sanic.core.domain.entities import ExternalRef
from framework_sanic.core.ports.connector import Connector, ConnectorCredential

from .models import CallActivity, Note


@runtime_checkable
class CallLogger(Protocol):
    async def upsert_call(self, cred: ConnectorCredential, activity: CallActivity) -> ExternalRef:
        """Create or update; ``activity.external_ref`` is set when the record already exists."""
        ...

    async def attach_recording(self, cred: ConnectorCredential, ref: ExternalRef, url: str) -> None: ...

    async def add_note(self, cred: ConnectorCredential, ref: ExternalRef, note: Note) -> None: ...

    async def find_by_reference(self, cred: ConnectorCredential, activity_key: str) -> ExternalRef | None: ...


class CallLoggingConnector(Connector, CallLogger, Protocol):
    """A connector that supports call_logging."""
