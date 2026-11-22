"""HubSpot's listeners in the whole framework (harness): what reaches HubSpot for Dialpad calls,
SMS and notes, with duplicates, shuffles, failures, settings and rematches."""

from __future__ import annotations

import json
import random
from typing import Any

import pytest

from fake_hubspot_api import FakeHubSpot
from framework_sanic.sdk import TOPIC_SYNC, OAuthTokens, Principal, QueueMessage
from framework_sanic.testing import NOW_MS, Harness, call_event, sms_event, start_harness
from framework_sanic_hubspot import REFERENCE_PROPERTY, HubSpotConnector
from framework_sanic_hubspot.activities import LoggedCall, LoggedSms
from framework_sanic_hubspot.listeners.calls import SYNC_JOB

USER = Principal(company_id="c1", user_id="c1-u1")
FEATURES = "hubspot.calls,hubspot.sms,hubspot.notes"


@pytest.fixture
def api() -> FakeHubSpot:
    api = FakeHubSpot()
    api.token = "ext-company"  # the company credential connect_company() stores
    return api


async def started(api: FakeHubSpot, **settings: Any) -> Harness:
    connector = HubSpotConnector(
        client_id="id", client_secret="s", api_base="https://api.test", http=api.client()
    )
    return await start_harness(connector, FEATURES=FEATURES, **settings)


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


def calls_by_key(api: FakeHubSpot) -> dict[str, dict[str, Any]]:
    return {props[REFERENCE_PROPERTY]: {"id": oid, **props} for oid, props in api.objects["calls"].items()}


def contact_of(api: FakeHubSpot, call_oid: str) -> str | None:
    return next((a[3] for a in api.associations if a[:3] == ("calls", call_oid, "contacts")), None)


# --- calls ------------------------------------------------------------------------------------
async def test_shuffled_duplicated_events_make_one_call(api: FakeHubSpot) -> None:
    ada = api.add_contact("Ada", "+14155550100")
    h = await started(api)
    await replay_call(h)
    await h.drain()
    (call,) = calls_by_key(api).values()
    assert call[REFERENCE_PROPERTY] == "dialpad:c1:call:1001" and call["hs_call_status"] == "COMPLETED"
    assert call["hs_call_recording_url"] == "https://rec/1001" and contact_of(api, call["id"]) == ada
    act = LoggedCall.from_record(await h.repo.get(LoggedCall.KIND, ("c1", "1001")))  # type: ignore[arg-type]
    assert act.contact is not None and act.contact.external_id == ada and act.owner_user_id == "c1-u1"
    logged = await h.services.entity_maps.get("c1", act.activity_key)
    assert logged is not None and (logged.external_type, logged.external_id) == ("calls", call["id"])
    assert [x async for x in h.repo.query("Outbox")] == []  # outbox fast path published + cleared


async def test_duplicate_sync_after_losing_the_map_creates_one_call(api: FakeHubSpot) -> None:
    h = await started(api)
    await replay_call(h, shuffle=False)
    await h.drain()
    # crash after the HubSpot create, before the local write: find_by_reference must save us
    await h.services.entity_maps.delete("c1", "dialpad:c1:call:1001")
    await h.repo.upsert(
        LoggedCall.KIND, ("c1", "1001"), lambda cur: {**cur, "synced_hash": None} if cur else None
    )
    for i in range(3):
        await h.queue.publish(sync_msg(n=i))
    await h.drain()
    assert len(api.objects["calls"]) == 1


def sync_msg(call_id: str = "1001", n: int = 0) -> QueueMessage:
    return QueueMessage(
        message_id=f"dup-{n}",
        topic=TOPIC_SYNC,
        company_id="c1",
        lane="live",
        job=SYNC_JOB,
        payload={"call_id": call_id},
    )


async def test_non_terminal_and_disabled_targets_are_not_logged(api: FakeHubSpot) -> None:
    h = await started(api)
    await h.webhook("c1", call_event("1", "ringing", NOW_MS + 1))
    await h.drain()
    await h.services.reconciler.reconcile("c1")
    await h.services.targets.set_override("c1", "office", "c1-office-1", False)  # inherited by users
    await h.webhook("c1", call_event("2", "hangup"))
    await h.drain()
    assert api.objects["calls"] == {}
    assert await h.repo.get(LoggedCall.KIND, ("c1", "1")) is not None  # folded, just not terminal
    assert await h.repo.get(LoggedCall.KIND, ("c1", "2")) is None


async def test_operator_leg_dropped_when_group_logging_enabled(api: FakeHubSpot) -> None:
    h = await started(api)
    await h.services.reconciler.reconcile("c1")
    dept = {"id": "c1-dept-sales", "type": "department"}
    await h.webhook("c1", call_event(call_id="2001", target=dept, operator={"id": "c1-u2", "name": "Op"}))
    await h.webhook(
        "c1", call_event(call_id="2002", target={"id": "c1-u2", "type": "user"}, entry_point_target=dept)
    )
    await h.drain()
    dept_act = await h.repo.get(LoggedCall.KIND, ("c1", "2001"))
    assert dept_act is not None and dept_act["owner_user_id"] == "c1-u2"
    assert await h.repo.get(LoggedCall.KIND, ("c1", "2002")) is None


async def test_call_settings(api: FakeHubSpot) -> None:
    h = await started(
        api,
        HUBSPOT_CALLS_DIRECTIONS="inbound",
        HUBSPOT_CALLS_SKIP_STATES="missed",
        HUBSPOT_CALLS_MIN_DURATION_SECONDS="90",
        HUBSPOT_CALLS_RECORDINGS="false",
    )
    for call_id, state, kw in [
        ("1", "hangup", {"duration": 120_000.0}),  # logged
        ("2", "missed", {}),  # skipped state
        ("3", "hangup", {"direction": "outbound", "duration": 120_000.0}),  # direction not logged
        ("4", "hangup", {}),  # 60 s < 90 s
    ]:
        await h.webhook("c1", call_event(call_id, state, **kw))
    await h.webhook("c1", call_event("1", "recording", NOW_MS + 9, recording_url=["https://rec/x"]))
    await h.drain()
    (call,) = calls_by_key(api).values()
    assert call[REFERENCE_PROPERTY] == "dialpad:c1:call:1" and "hs_call_recording_url" not in call


async def test_rate_limited_then_logged(api: FakeHubSpot) -> None:
    h = await started(api)
    await replay_call(h, shuffle=False)
    api.fail_with(429, 0)
    await h.drain()
    assert api.objects["calls"] == {}
    assert h.queue.pending(TOPIC_SYNC) >= 1  # retried later, not dropped
    api.failing = None
    await h.queue.publish(sync_msg(n=7))  # the delayed retry, delivered now
    await h.drain()
    assert len(api.objects["calls"]) == 1


async def test_parked_company_waits_then_logs(api: FakeHubSpot) -> None:
    h = await started(api)
    await h.services.companies.park("c1", "credential revoked")
    await replay_call(h, shuffle=False)
    await h.drain()
    assert await h.repo.get(LoggedCall.KIND, ("c1", "1001")) is not None  # events keep flowing
    assert api.objects["calls"] == {} and h.queue.pending(TOPIC_SYNC) >= 1  # parked, not dropped
    await h.services.companies.unpark("c1")
    await h.queue.publish(sync_msg(n=99))
    await h.drain()
    assert len(api.objects["calls"]) == 1


async def test_bad_user_credential_falls_back_to_company(api: FakeHubSpot) -> None:
    h = await started(api)
    await h.services.external_credentials.save("c1", "user", "c1-u1", OAuthTokens(access_token="user-token"))
    await replay_call(h, shuffle=False)
    await h.drain()  # HubSpot rejects the user token (401)
    user = await h.services.token_store.get_external("c1", "user", "c1-u1")
    assert user is not None and user.status == "bad"
    assert len(api.objects["calls"]) == 1


async def test_rematch_is_forward_only(api: FakeHubSpot) -> None:
    ada = api.add_contact("Ada", "+14155550100")
    bob = api.add_contact("Bob", "+14155550222")
    h = await started(api)
    await replay_call(h, "1001", shuffle=False)
    await h.drain()
    await h.services.contacts.rematch(USER, "phone:+14155550100", "contact", bob)
    await h.webhook(
        "c1", call_event("1001", "recording", NOW_MS + 90_000, recording_url=["https://rec/late"])
    )
    await replay_call(h, "1002", shuffle=False)
    await h.drain()
    calls = calls_by_key(api)
    assert contact_of(api, calls["dialpad:c1:call:1001"]["id"]) == ada
    assert contact_of(api, calls["dialpad:c1:call:1002"]["id"]) == bob
    assert calls["dialpad:c1:call:1001"]["hs_call_recording_url"] == "https://rec/late"


async def test_unknown_caller_rules(api: FakeHubSpot) -> None:
    h = await started(api)
    await replay_call(h, "1001", shuffle=False)
    await h.drain()
    (call,) = calls_by_key(api).values()
    assert contact_of(api, call["id"]) is None  # default: log without a contact (VERIFY)

    created = await started(api, HUBSPOT_CALLS_UNKNOWN_CALLER="create_contact")
    await replay_call(created, "2001", shuffle=False)
    await created.drain()
    assert len(api.objects["contacts"]) == 1

    skip_api = FakeHubSpot()
    skip_api.token = "ext-company"
    skipping = await started(skip_api, HUBSPOT_CALLS_UNKNOWN_CALLER="skip")
    await replay_call(skipping, "3001", shuffle=False)
    await skipping.drain()
    assert skip_api.objects["calls"] == {}


# --- SMS --------------------------------------------------------------------------------------
async def test_sms_text_reaches_hubspot_never_storage(api: FakeHubSpot) -> None:
    ada = api.add_contact("Ada", "+14155550100")
    h = await started(api)
    for _ in range(3):
        await h.webhook("c1", sms_event(sms_id=42, text="meet at noon"))
    await h.webhook("c1", sms_event(sms_id=43, status="failed"))  # first status seen is failed
    await h.drain()
    ((oid, sms),) = api.objects["communications"].items()
    assert sms["hs_communication_body"] == "meet at noon" and sms[REFERENCE_PROPERTY] == "dialpad:c1:sms:42"
    assert ("communications", oid, "contacts", ada) in api.associations
    act = LoggedSms.from_record(await h.repo.get(LoggedSms.KIND, ("c1", 42)))  # type: ignore[arg-type]
    assert act.synced and act.contact is not None
    assert await h.repo.get(LoggedSms.KIND, ("c1", 43)) is None
    stored = [r async for r in h.repo.query(LoggedSms.KIND)] + [r async for r in h.repo.query("Outbox")]
    assert "meet at noon" not in json.dumps(stored)


async def test_sms_settings(api: FakeHubSpot) -> None:
    h = await started(api, HUBSPOT_SMS_DIRECTIONS="inbound", HUBSPOT_SMS_INCLUDE_TEXT="false")
    await h.webhook("c1", sms_event(1, text="secret text"))
    await h.webhook("c1", sms_event(2, direction="outbound"))
    await h.drain()
    (sms,) = api.objects["communications"].values()
    assert sms[REFERENCE_PROPERTY] == "dialpad:c1:sms:1" and sms["hs_communication_body"] == ""


# --- notes ------------------------------------------------------------------------------------
NOTE = {"note_id": "note-0001", "call_id": "1001", "body": "call back Tuesday"}


async def post_note(h: Harness) -> Any:
    r = await h.request(
        "POST", "/notes", headers={"X-Dialpad-Id-Token": h.id_token(user_id="c1-u1")}, body=json.dumps(NOTE)
    )
    assert r.status == 202
    return json.loads(r.body)


async def deliver_waiting_note(h: Harness, n: int, *, attempt: int = 0) -> None:
    """The note job's delayed retry, delivered now (the memory queue would wait 30 s)."""
    await h.queue.publish(
        QueueMessage(
            message_id=f"note-retry-{n}",
            attempt=attempt,
            topic=TOPIC_SYNC,
            company_id="c1",
            lane="ui",
            job="hubspot.sync_note",
            payload={**NOTE, "company_id": "c1", "author_user_id": "c1-u1", "created_at": h.clock.now()},
        )
    )
    await h.drain()


async def test_note_waits_for_the_call_then_is_added_once(api: FakeHubSpot) -> None:
    h = await started(api)
    assert (await post_note(h))["synced"] is False
    await post_note(h)  # a client retry: a second job for the same note
    await h.drain()
    assert api.objects["notes"] == {}  # the call is not logged yet: the note waits
    await replay_call(h, shuffle=False)
    await h.drain()
    for n in range(3):  # the original job and the retry both run; and once more for good measure
        await deliver_waiting_note(h, n)
    (note_props,) = api.objects["notes"].values()
    assert note_props["hs_note_body"] == "call back Tuesday"
    (call_oid,) = api.objects["calls"]
    assert any(a[0] == "notes" and a[2:] == ("calls", call_oid) for a in api.associations)


async def test_note_outlives_a_long_call(api: FakeHubSpot) -> None:
    """Notes are written during the call; waiting for a long call must not use up attempts."""
    h = await started(api)
    await post_note(h)
    # A retry already at the worker's last attempt (30): counted, it would be dead-lettered now.
    await deliver_waiting_note(h, 1, attempt=29)
    assert not [m for m in h.queue.published if m.topic == "deadletter"]
    await replay_call(h, shuffle=False)
    await h.drain()
    await deliver_waiting_note(h, 3)
    assert len(api.objects["notes"]) == 1


async def test_note_for_a_call_never_logged_gives_up_after_a_day(api: FakeHubSpot) -> None:
    h = await started(api)
    await post_note(h)
    await h.drain()
    payload = {**NOTE, "company_id": "c1", "author_user_id": "c1-u1", "created_at": h.clock.now()}
    h.clock.advance(25 * 3600)
    await h.queue.publish(
        QueueMessage(
            message_id="late",
            topic=TOPIC_SYNC,
            company_id="c1",
            lane="ui",
            job="hubspot.sync_note",
            payload=payload,
        )
    )
    await h.drain()
    assert [m.payload["_reason"] for m in h.queue.published if m.topic == "deadletter"] == [
        "call was never logged"
    ]
