"""What a connector must implement for ``sms_logging``.

Idempotency: connectors store the activity key as an external reference on the external record;
``find_by_reference`` is used before creating, so retries never duplicate.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from framework_sanic.core.domain.entities import ExternalRef
from framework_sanic.core.ports.connector import Connector, ConnectorCredential

from .models import SmsLog


@runtime_checkable
class SmsLogger(Protocol):
    async def log_sms(self, cred: ConnectorCredential, activity: SmsLog) -> ExternalRef:
        """``activity.text`` is the transiently decrypted message text: send it, never store it."""
        ...

    async def find_by_reference(self, cred: ConnectorCredential, activity_key: str) -> ExternalRef | None: ...


class SmsLoggingConnector(Connector, SmsLogger, Protocol):
    """A connector that supports sms_logging."""
