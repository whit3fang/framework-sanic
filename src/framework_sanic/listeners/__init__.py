"""Where an integration receives Dialpad activity: listener base classes and activity models.

The framework receives, verifies and buffers Dialpad webhooks, subscribes to the event kinds a
listener needs, and delivers each event at least once, per company, with retries driven by the
returned ``JobResult``. *What happens* to a call, an SMS or a note is the integration's: subclass a
listener, implement its method, and register it as a feature (``framework_sanic.features`` entry
point, enabled with ``FEATURES``)::

    class AcmeCalls(CallListener):
        name = "acme.calls"

        async def on_call(self, event: InboundEvent) -> JobResult:
            call = CallActivity.from_event(event.call, company_id=event.company_id)
            ...
            return ACK
"""

from __future__ import annotations

from .base import CallListener, NoteListener, SmsListener
from .models import CallActivity, NoteIn, NoteOut, SmsActivity

__all__ = ["CallActivity", "CallListener", "NoteIn", "NoteListener", "NoteOut", "SmsActivity", "SmsListener"]
