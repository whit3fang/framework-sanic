"""Dialpad webhook endpoint (buffer-first accept path).

The signature is verified by the webhook middleware. This handler only parses, encrypts SMS text,
publishes one message per registered event handler to the durable queue and returns 2xx. If the queue publish fails the response is 5xx
(Dialpad retries 5xx); a deliverable event never gets a 4xx/429.
"""

from __future__ import annotations

from pydantic import BaseModel

from framework_sanic.core.intake import accept_event

from ..routes import RequestContext, route


class WebhookIn(BaseModel):
    company_id: str


class Accepted(BaseModel):
    accepted: bool = True
    kind: str
    deliveries: int = 0  # handlers the event was queued for (0 if no enabled feature handles it)


@route("POST", "/webhooks/dialpad/{company_id}/events", auth="webhook")
async def dialpad_webhook(req: WebhookIn, ctx: RequestContext) -> Accepted:
    assert ctx.webhook_payload is not None
    s = ctx.services
    routes = s.features.event_routes if s.features is not None else {}
    result = await accept_event(req.company_id, ctx.webhook_payload, s.cipher, s.queue, s.clock.now(), routes)
    return Accepted(kind=result.kind, deliveries=result.deliveries)
