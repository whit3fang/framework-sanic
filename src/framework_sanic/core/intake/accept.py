"""Webhook accept path (buffer-first). This is where the framework's responsibility for an event
ends: verified, parsed, made safe to queue, and durably handed to every handler that asked for it.

verify signature (middleware) -> parse -> encrypt SMS text -> publish one message per registered
handler -> 2xx. Nothing here touches Redis or the database, so the accept path keeps working while
both are down.

Fan-out happens here (from in-process config, no I/O) so each handler gets its own queue message
and therefore its own retries, backoff and deadlettering: a handler that keeps failing never makes
another handler see the event twice.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from ..domain.events import CallEvent, SmsEvent, classify_event
from ..domain.records import Lane, stable_hash
from ..ports.queue import TOPIC_DEADLETTER, TOPIC_EVENTS, Queue, QueueMessage
from ..services.crypto import EnvelopeCipher
from .delivery import EventKind, sms_aad

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Accepted:
    kind: str
    message_ids: list[str]
    deliveries: int  # handlers the event was queued for (unparseable events go to deadletter: 0)


async def build_event_messages(
    company_id: str,
    payload: dict[str, Any],
    cipher: EnvelopeCipher,
    now: float,
    handlers: Mapping[EventKind, Sequence[str]],
) -> tuple[str, list[QueueMessage]]:
    """Return the event kind and the messages to publish (one per handler job, possibly none)."""
    kind = classify_event(payload)
    try:
        if kind == "call":
            call = CallEvent.model_validate(payload)
            base_id = stable_hash(call.call_id, call.state, call.event_timestamp)
            body: dict[str, Any] = {"event": call.model_dump(mode="json")}
            return kind, _fan_out(company_id, "call", base_id, body, now, handlers)
        if kind == "sms":
            sms = SmsEvent.model_validate(payload)
            jobs = handlers.get("sms", ())
            # Encrypt only if someone will receive it; the text exists only inside queue messages.
            text_enc = (
                await cipher.encrypt_str(sms.text, sms_aad(company_id, sms.id))
                if sms.text is not None and jobs
                else None
            )
            body = {"event": sms.model_dump(mode="json", exclude={"text"}), "text_enc": text_enc}
            return kind, _fan_out(company_id, "sms", str(sms.id), body, now, handlers)
    except ValidationError as exc:
        log.warning("unparseable %s event for %s: %s", kind, company_id, exc.errors()[:3])
    # Signed but unrecognised/unparseable: keep it for inspection, never 4xx (Dialpad won't retry).
    # Only the field names are kept; values could contain message text.
    return kind or "unknown", [
        QueueMessage(
            message_id=f"dlq:{company_id}:{stable_hash(sorted(payload), now)}",
            topic=TOPIC_DEADLETTER,
            company_id=company_id,
            lane=Lane.BACKFILL,
            job="unparseable_event",
            payload={"fields": sorted(payload)[:50], "kind": kind},
            enqueued_at=now,
        )
    ]


def _fan_out(
    company_id: str,
    kind: EventKind,
    base_id: str,
    body: dict[str, Any],
    now: float,
    handlers: Mapping[EventKind, Sequence[str]],
) -> list[QueueMessage]:
    jobs = handlers.get(kind, ())
    if not jobs:
        log.debug("no handler registered for %s events; dropping (company %s)", kind, company_id)
    return [
        QueueMessage(
            message_id=f"{job}:{company_id}:{base_id}",
            topic=TOPIC_EVENTS,
            company_id=company_id,
            lane=Lane.LIVE,
            job=job,
            payload=body,
            enqueued_at=now,
        )
        for job in jobs
    ]


async def accept_event(
    company_id: str,
    payload: dict[str, Any],
    cipher: EnvelopeCipher,
    queue: Queue,
    now: float,
    handlers: Mapping[EventKind, Sequence[str]],
) -> Accepted:
    """Publish the event to every registered handler. Raises if a publish fails (-> 5xx, Dialpad
    retries); a handler may therefore see the same event more than once and must be idempotent."""
    kind, messages = await build_event_messages(company_id, payload, cipher, now, handlers)
    for message in messages:
        await queue.publish(message)
    return Accepted(
        kind=kind,
        message_ids=[m.message_id for m in messages],
        deliveries=sum(1 for m in messages if m.topic == TOPIC_EVENTS),
    )
