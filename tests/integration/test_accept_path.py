"""Gate 5: webhook accept path is buffer-first and works with the cache and the database stopped."""

from __future__ import annotations

import json

from framework_sanic.core.ports.queue import TOPIC_DEADLETTER, TOPIC_EVENTS
from framework_sanic.testing import Harness, call_event, sms_event


async def test_accepts_with_cache_and_db_down(harness: Harness) -> None:
    h = harness
    h.cache.down = True
    h.repo.down = True
    r = await h.webhook("c1", call_event())
    assert r.status == 200
    r = await h.webhook("c1", sms_event(text="secret text"))
    assert r.status == 200
    topics = [m.topic for m in h.queue.published]
    assert topics == [TOPIC_EVENTS, TOPIC_EVENTS]


async def test_sms_text_encrypted_in_queue(harness: Harness) -> None:
    h = harness
    await h.webhook("c1", sms_event(text="the eagle has landed"))
    (msg,) = h.queue.published
    raw = msg.model_dump_json()
    assert "the eagle has landed" not in raw
    assert msg.payload["text_enc"] and "text" not in msg.payload["event"]
    from framework_sanic.core.intake import sms_aad

    clear = await h.services.cipher.decrypt_str(msg.payload["text_enc"], sms_aad("c1", 555))
    assert clear == "the eagle has landed"


async def test_signature_failures(harness: Harness) -> None:
    h = harness
    path = "/webhooks/dialpad/c1/events"
    assert (await h.request("POST", path, body=json.dumps(call_event()))).status == 401
    assert (await h.request("POST", path, body=h.sign("c2", call_event()))).status == 401
    assert (await h.request("POST", path, body=b"")).status == 401
    tampered = h.sign("c1", call_event())
    head, payload, sig = tampered.split(".")
    assert (await h.request("POST", path, body=f"{head}.{payload}.{sig[::-1]}")).status == 401
    assert not h.queue.published


async def test_queue_down_returns_5xx_so_dialpad_retries(harness: Harness) -> None:
    h = harness
    h.queue.down = True
    r = await h.webhook("c1", call_event())
    assert r.status == 503


async def test_unparseable_signed_event_is_dead_lettered_not_rejected(harness: Harness) -> None:
    h = harness
    r = await h.webhook("c1", {"id": "not-an-int", "text": "secret", "message_status": "sent"})
    assert r.status == 200
    (msg,) = h.queue.published
    assert msg.topic == TOPIC_DEADLETTER and "secret" not in msg.model_dump_json()


async def test_transcripts_never_enqueued(harness: Harness) -> None:
    h = harness
    await h.webhook("c1", call_event(transcription_text="private words", ai_summary="x"))
    assert "private words" not in h.queue.published[0].model_dump_json()
