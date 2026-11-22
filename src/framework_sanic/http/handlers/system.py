from __future__ import annotations

from pydantic import BaseModel

from framework_sanic.core.ports.queue import TOPIC_EVENTS, TOPIC_SYNC

from ..routes import RawResponse, RequestContext, route


class Empty(BaseModel):
    pass


class Health(BaseModel):
    status: str


@route("GET", "/healthz", auth="none")
async def healthz(req: Empty, ctx: RequestContext) -> Health:
    return Health(status="ok")


@route("GET", "/readyz", auth="none")
async def readyz(req: Empty, ctx: RequestContext) -> RawResponse:
    """Ready when the durable queue is reachable (the webhook accept path needs nothing else)."""
    s = ctx.services
    queue_ok = await s.queue.ping()
    checks = {"queue": queue_ok, "repository": await s.repo.ping(), "cache": await s.cache.ping()}
    stats = {}
    for topic in (TOPIC_EVENTS, TOPIC_SYNC):
        st = await s.queue.stats(topic)
        stats[topic] = {"depth": st.depth, "oldest_age_seconds": round(st.oldest_age_seconds, 3)}
    import json

    return RawResponse(
        status=200 if queue_ok else 503,
        body=json.dumps({"ready": queue_ok, "checks": checks, "queues": stats}),
        content_type="application/json",
    )
