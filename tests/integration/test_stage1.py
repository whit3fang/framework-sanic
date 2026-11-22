"""Gate 6: stage 1 merge (calls fold, SMS insert-once, department legs, outbox)."""

from __future__ import annotations

import random

from framework_sanic.core.domain.entities import OutboxEntry
from framework_sanic.core.ports.queue import TOPIC_SYNC
from framework_sanic.features.call_logging.models import CallActivity
from framework_sanic.features.sms_logging.models import SmsActivity
from framework_sanic.testing import NOW_MS, Harness, call_event, sms_event


async def activity(h: Harness, call_id: str) -> CallActivity | None:
    rec = await h.repo.get(CallActivity.KIND, ("c1", call_id))
    return CallActivity.from_record(rec) if rec else None


async def run_stage1(h: Harness) -> None:
    from framework_sanic.workers.runtime import WorkerRuntime

    await WorkerRuntime([p for p in h.workers.pools if p.topic == "events"]).drain()


async def test_shuffled_duplicated_events_converge(harness: Harness) -> None:
    h = harness
    await h.connect_company()
    events = [
        call_event(state="calling", ts=NOW_MS + 1_000),
        call_event(state="ringing", ts=NOW_MS + 2_000),
        call_event(state="connected", ts=NOW_MS + 3_000),
        call_event(state="hangup", ts=NOW_MS + 9_000),
        call_event(state="recording", ts=NOW_MS + 9_500, recording_url=["https://rec/1"]),
    ]
    events += [random.choice(events) for _ in range(4)]
    random.shuffle(events)
    for e in events:
        assert (await h.webhook("c1", e)).status == 200
    await run_stage1(h)
    act = await activity(h, "1001")
    assert act is not None and act.state == "hangup" and act.terminal
    assert act.recording_urls == ["https://rec/1"] and act.owner_user_id == "c1-u1"
    sync_msgs = [m for m in h.queue.published if m.topic == TOPIC_SYNC]
    assert sync_msgs and all(m.job == "sync.call" for m in sync_msgs)
    assert [x async for x in h.repo.query(OutboxEntry.KIND)] == []  # fast path published + cleared


async def test_works_without_redis(harness: Harness) -> None:
    h = harness
    await h.connect_company()
    h.cache.down = True
    for e in [
        call_event(state="hangup", ts=NOW_MS + 5),
        call_event(state="ringing", ts=NOW_MS + 1),
        call_event(state="hangup", ts=NOW_MS + 5),
    ]:
        await h.webhook("c1", e)
    await run_stage1(h)
    act = await activity(h, "1001")
    assert act is not None and act.state == "hangup"


async def test_non_terminal_only_does_not_sync(harness: Harness) -> None:
    h = harness
    await h.connect_company()
    await h.webhook("c1", call_event(state="ringing", ts=NOW_MS + 1))
    await run_stage1(h)
    assert (await activity(h, "1001")) is not None
    assert not [m for m in h.queue.published if m.topic == TOPIC_SYNC]


async def test_operator_leg_dropped_when_group_logging_enabled(harness: Harness) -> None:
    h = harness
    await h.connect_company()
    await h.services.reconciler.reconcile("c1")
    dept = {"id": "c1-dept-sales", "type": "department"}
    # department leg: owner from the operator JSON (VERIFY shape)
    await h.webhook("c1", call_event(call_id="2001", target=dept, operator={"id": "c1-u2", "name": "Op"}))
    # operator leg of the same call (different call id)
    await h.webhook(
        "c1", call_event(call_id="2002", target={"id": "c1-u2", "type": "user"}, entry_point_target=dept)
    )
    await run_stage1(h)
    dept_act = await activity(h, "2001")
    assert dept_act is not None and dept_act.owner_user_id == "c1-u2" and dept_act.is_department_leg
    assert await activity(h, "2002") is None

    # disable department logging -> operator legs are logged instead
    await h.services.targets.set_override("c1", "department", "c1-dept-sales", False)
    await h.webhook(
        "c1", call_event(call_id="2003", target={"id": "c1-u2", "type": "user"}, entry_point_target=dept)
    )
    await h.webhook("c1", call_event(call_id="2004", target=dept))
    await run_stage1(h)
    assert await activity(h, "2003") is not None
    assert await activity(h, "2004") is None


async def test_logging_disabled_target_dropped(harness: Harness) -> None:
    h = harness
    await h.connect_company()
    await h.services.reconciler.reconcile("c1")
    await h.services.targets.set_override("c1", "office", "c1-office-1", False)  # inherited by users
    await h.webhook("c1", call_event(call_id="3001"))
    await run_stage1(h)
    assert await activity(h, "3001") is None


async def test_sms_insert_once(harness: Harness) -> None:
    h = harness
    await h.connect_company()
    await h.webhook("c1", sms_event(sms_id=1, text="zebra-unique"))
    await h.webhook("c1", sms_event(sms_id=1, text="zebra-unique"))
    await h.webhook("c1", sms_event(sms_id=2, status="failed"))
    await run_stage1(h)
    one = await h.repo.get(SmsActivity.KIND, ("c1", 1))
    assert one is not None and "text" not in one and "zebra-unique" not in str(one)
    assert await h.repo.get(SmsActivity.KIND, ("c1", 2)) is None  # first seen status failed -> skipped
    # a later non-failed event for an already-skipped id inserts (status may differ on retry)
    sync_msgs = [m for m in h.queue.published if m.job == "sync.sms"]
    assert len(sync_msgs) == 2 and all(
        m.payload["text_enc"] for m in sync_msgs
    )  # duplicates harmless (stage 2 dedupes)


async def test_outbox_relay_republishes(harness: Harness) -> None:
    from framework_sanic.core.services.outbox import OutboxRelay

    h = harness
    entry = OutboxEntry(
        company_id="c1",
        outbox_id="sync.call:c1:9:x",
        topic=TOPIC_SYNC,
        lane="live",
        job="sync.call",
        payload={"call_id": "9"},
        created_at=h.clock.now() - 60,
    )
    await h.repo.upsert(OutboxEntry.KIND, entry.key(), lambda cur: entry.to_record())
    assert await OutboxRelay(h.repo, h.queue, h.clock).relay_once() == 1
    assert h.queue.published[-1].job == "sync.call"
    assert await h.repo.get(OutboxEntry.KIND, entry.key()) is None
