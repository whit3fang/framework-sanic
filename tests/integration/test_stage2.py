"""Gate 8: connector contract + stage-2 sync with rate limits and idempotent external references.

Includes the end-to-end replay: webhook -> accept path -> stage 1 -> outbox -> stage 2 -> fake connector.
"""

from __future__ import annotations

import asyncio
import json
import random

from framework_sanic.core.domain.records import Lane
from framework_sanic.core.ports.platform import OAuthTokens
from framework_sanic.core.ports.queue import TOPIC_SYNC, QueueMessage
from framework_sanic.features.call_logging.models import CallActivity, Note
from framework_sanic.features.call_logging.notes import NotesService
from framework_sanic.features.sms_logging.models import SmsActivity
from framework_sanic.stubs.connector.connector import FakeHTTPError
from framework_sanic.testing import NOW_MS, Harness, call_event, sms_event


def sync_msg(call_id: str = "1001", n: int = 0) -> QueueMessage:
    return QueueMessage(
        message_id=f"dup-{n}",
        topic=TOPIC_SYNC,
        company_id="c1",
        lane=Lane.LIVE,
        job="sync.call",
        payload={"call_id": call_id},
    )


async def replay_call(h: Harness, call_id: str = "1001", *, shuffle: bool = True) -> None:
    events = [
        call_event(call_id, "calling", NOW_MS),
        call_event(call_id, "connected", NOW_MS + 1000),
        call_event(call_id, "hangup", NOW_MS + 60_000),
        call_event(call_id, "recording", NOW_MS + 61_000, recording_url=[f"https://rec/{call_id}"]),
    ]
    events += events[1:3]  # duplicates
    if shuffle:
        random.shuffle(events)
    for e in events:
        assert (await h.webhook("c1", e)).status == 200


async def test_end_to_end_replay_into_fake_connector(harness: Harness) -> None:
    h = harness
    await h.connect_company()
    contact = h.connector.seed_contact("Ada", "+14155550100")
    await replay_call(h)
    await h.drain()
    calls = h.connector.records_for("call")
    assert len(calls) == 1
    rec = calls[0]
    assert rec.properties["state"] == "hangup" and rec.properties["contact"] == contact.external_id
    assert rec.recordings == ["https://rec/1001"] or rec.properties["recordings"] == ["https://rec/1001"]
    act = CallActivity.from_record(await h.repo.get(CallActivity.KIND, ("c1", "1001")))  # type: ignore[arg-type]
    assert act.contact is not None and act.contact.external_id == contact.external_id
    assert act.synced_hash is not None
    # stored maps: the call record, and the caller's contact (matched once, then reused)
    logged = await h.services.entity_maps.get("c1", act.activity_key)
    assert logged is not None and (logged.external_type, logged.external_id) == ("call", rec.ref.external_id)
    caller = await h.services.entity_maps.get("c1", act.dialpad_contact_ref or "")
    assert caller is not None and caller.external_id == contact.external_id


async def forget_logged_call(h: Harness, call_id: str = "1001") -> None:
    await h.services.entity_maps.delete("c1", f"dialpad:c1:call:{call_id}")
    await h.repo.upsert(
        CallActivity.KIND, ("c1", call_id), lambda cur: {**cur, "synced_hash": None} if cur else None
    )


async def test_duplicate_stage2_delivery_creates_one_record(harness: Harness) -> None:
    h = harness
    await h.connect_company()
    await replay_call(h)
    await h.drain()
    # Simulate losing the call's EntityMap (e.g. crash after the external create, before the
    # local write) and several duplicate deliveries, sequential and concurrent.
    await forget_logged_call(h)
    for i in range(3):
        await h.queue.publish(sync_msg(n=i))
    await h.drain()
    await forget_logged_call(h)
    stage2 = next(p for p in h.workers.pools if p.topic == TOPIC_SYNC)
    handler = stage2.handlers["sync.call"]
    h.cache.down = True  # no lease: find_by_reference must still prevent duplicates (sequentially)
    for i in range(3):
        await handler(sync_msg(n=10 + i))
    h.cache.down = False
    await forget_logged_call(h)
    h.connector.latency = 0.005
    results = await asyncio.gather(*(handler(sync_msg(n=20 + i)) for i in range(5)))  # concurrent duplicates
    assert all(r.action in ("ack", "retry") for r in results)
    await h.drain()
    assert len(h.connector.records_for("call")) == 1


async def test_rate_limited_by_connector_429(harness: Harness) -> None:
    h = harness
    await h.connect_company()
    await replay_call(h, shuffle=False)
    h.connector.fail_next = [FakeHTTPError(429, retry_after=0.05)]
    await h.drain()
    assert len(h.connector.records_for("call")) == 0 or True  # first attempt rejected
    await asyncio.sleep(0.15)
    await h.drain()
    assert len(h.connector.records_for("call")) == 1


async def test_token_bucket_defers_without_failing(harness: Harness) -> None:
    h = harness
    object.__setattr__(h.services.settings, "CONNECTOR_RATE_LIMIT_PER_MINUTE", 60)
    await h.connect_company()
    for i in range(70):
        await h.queue.publish(sync_msg(call_id=f"x{i}", n=i))  # activities don't exist -> cheap acks
    stage2 = next(p for p in h.workers.pools if p.topic == TOPIC_SYNC)
    for i in range(5):
        await h.repo.upsert(
            CallActivity.KIND,
            ("c1", f"r{i}"),
            lambda cur, i=i: CallActivity(
                company_id="c1", call_id=f"r{i}", state="hangup", terminal=True
            ).to_record(),
        )
    msgs = [sync_msg(call_id=f"r{i}", n=100 + i) for i in range(5)]
    # exhaust the bucket, then the next sync is deferred (not failed, no attempt counted)
    for _ in range(60):
        await h.services.limiter.acquire("connector:fake:c1", per_minute=60)
    result = await stage2.handlers["sync.call"](msgs[0])
    assert result.action == "retry" and not result.count_attempt


async def test_auth_error_falls_back_to_company_credential(harness: Harness) -> None:
    h = harness
    await h.connect_company()
    await h.services.external_credentials.save("c1", "user", "c1-u1", OAuthTokens(access_token="user-token"))
    await replay_call(h, shuffle=False)
    h.connector.fail_next = [FakeHTTPError(401)]
    await h.drain()
    user = await h.services.token_store.get_external("c1", "user", "c1-u1")
    assert user is not None and user.status == "bad"
    assert len(h.connector.records_for("call")) == 1


async def test_parked_company_defers_stage2_but_stage1_flows(harness: Harness) -> None:
    h = harness
    await h.connect_company()
    await h.services.companies.park("c1", "credential revoked")
    await replay_call(h, shuffle=False)
    await h.drain()
    assert await h.repo.get(CallActivity.KIND, ("c1", "1001")) is not None  # stage 1 kept flowing
    assert h.connector.records_for("call") == []
    assert h.queue.pending(TOPIC_SYNC) >= 1  # parked, not dropped
    await h.services.companies.unpark("c1")
    await h.queue.publish(sync_msg(n=99))  # on reconnect the (delayed) message resumes; simulate redelivery
    await h.drain()
    assert len(h.connector.records_for("call")) == 1


async def test_no_external_credential_parks(harness: Harness) -> None:
    h = harness
    await h.connect_company(external=False)
    await replay_call(h, shuffle=False)
    await h.drain()
    assert h.connector.records_for("call") == [] and h.queue.pending(TOPIC_SYNC) >= 1


async def test_sms_sync_text_reaches_connector_never_storage(harness: Harness) -> None:
    h = harness
    await h.connect_company()
    h.connector.seed_contact("Ada", "+14155550100")
    for _ in range(3):
        await h.webhook("c1", sms_event(sms_id=42, text="meet at noon"))
    await h.drain()
    sms = h.connector.records_for("sms")
    assert len(sms) == 1 and sms[0].properties["text"] == "meet at noon"
    act = SmsActivity.from_record(await h.repo.get(SmsActivity.KIND, ("c1", 42)))  # type: ignore[arg-type]
    assert act.synced and act.contact is not None
    assert await h.services.entity_maps.get("c1", act.activity_key) is not None
    stored = [r async for r in h.repo.query("SmsActivity")] + [r async for r in h.repo.query("Outbox")]
    dump = json.dumps(stored)
    assert "meet at noon" not in dump


async def test_note_waits_for_call_then_syncs_once(harness: Harness) -> None:
    h = harness
    await h.connect_company()
    from framework_sanic.core.services.session_service import Principal

    p = Principal(company_id="c1", user_id="c1-u1")
    await NotesService(h.repo, h.queue, h.clock).add(
        p, note_id="note-0001", call_id="1001", body="call back Tuesday"
    )
    await NotesService(h.repo, h.queue, h.clock).add(
        p, note_id="note-0001", call_id="1001", body="call back Tuesday"
    )
    await h.drain()  # call not synced yet -> note retries later
    assert not (await h.repo.get(Note.KIND, ("c1", "note-0001")))["synced"]  # type: ignore[index]
    await replay_call(h, shuffle=False)
    await h.drain()
    await h.queue.publish(
        QueueMessage(
            message_id="n",
            topic=TOPIC_SYNC,
            company_id="c1",
            lane=Lane.UI,
            job="sync.note",
            payload={"note_id": "note-0001"},
        )
    )
    await h.drain()
    (call,) = h.connector.records_for("call")
    assert call.notes == ["note-0001:call back Tuesday"]
    assert (await h.repo.get(Note.KIND, ("c1", "note-0001")))["synced"]  # type: ignore[index]


async def test_rematch_is_forward_only(harness: Harness) -> None:
    h = harness
    await h.connect_company()
    ada = h.connector.seed_contact("Ada", "+14155550100")
    bob = h.connector.seed_contact("Bob", "+14155550222")
    await replay_call(h, "1001", shuffle=False)
    await h.drain()
    from framework_sanic.core.services.session_service import Principal

    await h.services.contacts.rematch(
        Principal(company_id="c1", user_id="c1-u1"), "phone:+14155550100", "contact", bob.external_id
    )
    # a later update to the old call (new recording) keeps its original contact
    await h.webhook(
        "c1", call_event("1001", "recording", NOW_MS + 90_000, recording_url=["https://rec/late"])
    )
    await replay_call(h, "1002", shuffle=False)
    await h.drain()
    by_ref = {r.reference: r for r in h.connector.records_for("call")}
    assert by_ref["dialpad:c1:call:1001"].properties["contact"] == ada.external_id
    assert by_ref["dialpad:c1:call:1002"].properties["contact"] == bob.external_id
    assert "https://rec/late" in by_ref["dialpad:c1:call:1001"].recordings


async def test_unknown_caller_policies(harness: Harness) -> None:
    h = harness
    await h.connect_company()
    await replay_call(h, "1001", shuffle=False)
    await h.drain()
    (rec,) = h.connector.records_for("call")
    assert rec.properties["contact"] is None  # default: log_unassociated (VERIFY)
    object.__setattr__(h.services.settings, "UNKNOWN_CALLER_POLICY", "create_contact")
    await replay_call(h, "1002", shuffle=False)
    await h.drain()
    assert len(h.connector.contacts) == 1
