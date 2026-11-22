"""Delivery of received events to integration handlers.

The framework guarantees: at-least-once delivery, per-company sharding and fairness, lanes, and
retry/backoff/deadletter driven by the handler's ``JobResult``. What a handler *does* with an
event is entirely up to the integration.

    async def on_call(event: InboundEvent) -> JobResult: ...
    ctx.on_event("call", on_call)
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from pydantic import ValidationError

from ..domain.events import CallEvent, SmsEvent
from ..features.jobs import JobResult, deadletter
from ..ports.queue import QueueMessage
from ..services.crypto import EnvelopeCipher

EventKind = Literal["call", "sms"]
EVENT_KINDS: tuple[EventKind, ...] = ("call", "sms")


@dataclass(frozen=True)
class InboundEvent:
    """A verified, parsed Dialpad event as delivered to one handler."""

    kind: EventKind
    company_id: str
    message: QueueMessage
    call: CallEvent | None = None
    sms: SmsEvent | None = None  # ``text`` is always None here; see ``sms_text``
    text_enc: str | None = None  # encrypted SMS text, safe to forward in another queue message
    _cipher: EnvelopeCipher | None = field(default=None, repr=False, compare=False)

    @property
    def attempt(self) -> int:
        return self.message.attempt

    @property
    def received_at(self) -> float:
        return self.message.enqueued_at

    async def sms_text(self) -> str | None:
        """Decrypt the SMS text. Use it transiently; never store it."""
        if self.sms is None or self.text_enc is None or self._cipher is None:
            return None
        return await decrypt_sms_text(self._cipher, self.company_id, self.sms.id, self.text_enc)


def sms_aad(company_id: str, sms_id: int) -> str:
    """Associated data binding an encrypted SMS text to its company and message."""
    return f"sms:{company_id}:{sms_id}"


async def decrypt_sms_text(cipher: EnvelopeCipher, company_id: str, sms_id: int, text_enc: str) -> str:
    return await cipher.decrypt_str(text_enc, sms_aad(company_id, sms_id))


EventHandler = Callable[[InboundEvent], Awaitable[JobResult]]


def parse_inbound(kind: EventKind, msg: QueueMessage, cipher: EnvelopeCipher | None) -> InboundEvent:
    """Raises ``ValidationError`` if the queued payload no longer parses."""
    raw = msg.payload.get("event", {})
    if kind == "call":
        return InboundEvent("call", msg.company_id, msg, call=CallEvent.model_validate(raw), _cipher=cipher)
    return InboundEvent(
        "sms",
        msg.company_id,
        msg,
        sms=SmsEvent.model_validate(raw),
        text_enc=msg.payload.get("text_enc"),
        _cipher=cipher,
    )


def event_job(
    kind: EventKind, handler: EventHandler, cipher: Callable[[], EnvelopeCipher | None]
) -> Callable[[QueueMessage], Awaitable[JobResult]]:
    """Adapt an ``EventHandler`` to the worker's job contract."""

    async def job(msg: QueueMessage) -> JobResult:
        try:
            event = parse_inbound(kind, msg, cipher())
        except ValidationError as exc:
            return deadletter(f"unparseable queued {kind} event: {exc.errors()[:1]}")
        return await handler(event)

    return job
