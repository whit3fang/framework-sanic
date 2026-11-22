"""Framework boundary: the framework receives events and delivers them to whichever features
registered handlers; what happens next belongs to the feature."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

import pytest

from framework_sanic.adapters.queue.memory import MemoryQueue
from framework_sanic.bootstrap import build_services, startup
from framework_sanic.core.domain.errors import ConfigurationError
from framework_sanic.core.ports.queue import TOPIC_EVENTS
from framework_sanic.stubs.connector.connector import FakeConnector
from framework_sanic.testing import Harness, build_harness, call_event, make_settings, sms_event
from tests.support import echo_feature
from tests.support.echo_feature import STATE, EchoState

ECHO = "tests.support.echo_feature"


@pytest.fixture(autouse=True)
def reset_echo() -> None:
    fresh = EchoState()
    STATE.kinds, STATE.received, STATE.texts, STATE.fail_times = (
        fresh.kinds,
        fresh.received,
        fresh.texts,
        fresh.fail_times,
    )


async def started(**settings: str) -> Harness:
    h = build_harness(**settings)
    await startup(h.services, schedules=False)
    return h


def body(r: Any) -> Any:
    return json.loads(r.body)


def sub_kinds(h: Harness) -> Counter[str]:
    return Counter(s.kind for s in h.platform.subs.get("c1", {}).values())


async def test_no_features_accepts_but_delivers_nothing() -> None:
    h = await started(FEATURES="")
    r = await h.webhook("c1", call_event())
    assert r.status == 200 and body(r)["deliveries"] == 0
    assert h.queue.published == []
    await h.connect_company()
    await h.services.reconciler.reconcile("c1")
    assert sub_kinds(h) == Counter()  # nothing handles events -> nothing subscribed


async def test_subscriptions_follow_registered_handlers() -> None:
    h = await started(FEATURES=ECHO)  # echo handles calls only
    await h.connect_company()
    await h.services.reconciler.reconcile("c1")
    assert set(sub_kinds(h)) == {"call"}
    assert body(await h.webhook("c1", sms_event()))["deliveries"] == 0


async def test_fan_out_gives_each_feature_its_own_message_and_retries() -> None:
    h = await started(FEATURES=f"call_logging,{ECHO}")
    await h.connect_company()
    STATE.fail_times = 2
    r = await h.webhook("c1", call_event("77", "hangup"))
    assert body(r) == {"accepted": True, "kind": "call", "deliveries": 2}
    jobs = sorted(m.job for m in h.queue.published if m.topic == TOPIC_EVENTS)
    assert jobs == ["event.call:call_logging", "event.call:echo"]
    await h.drain()
    assert len(STATE.received) == 3  # failed twice, retried by the framework, then acked
    assert [e.attempt for e in STATE.received] == [0, 1, 2]
    assert STATE.received[-1].call is not None and STATE.received[-1].call.call_id == "77"
    # echo's retries never re-ran call_logging: exactly one external record.
    assert [r.reference for r in h.connector.records.values()] == ["dialpad:c1:call:77"]


async def test_handler_can_decrypt_sms_text_which_is_never_queued_in_clear() -> None:
    STATE.kinds = ("sms",)
    h = await started(FEATURES=ECHO)
    await h.webhook("c1", sms_event(text="meet at noon"))
    assert all("meet at noon" not in m.model_dump_json() for m in h.queue.published)
    await h.drain()
    assert STATE.texts == ["meet at noon"]
    assert STATE.received[0].sms is not None and STATE.received[0].sms.text is None


@pytest.mark.parametrize(("feature", "port"), [("call_logging", "CallLogger"), ("sms_logging", "SmsLogger")])
async def test_logging_features_require_their_connector_port(feature: str, port: str) -> None:
    class ContactsOnly:  # a connector with no activity-writing methods
        name = "contacts_only"
        auth = FakeConnector().auth
        capabilities: set[str] = set()

    with pytest.raises(ConfigurationError, match=port):
        build_services(make_settings(FEATURES=feature), queue=MemoryQueue(2), connector=ContactsOnly())


async def test_call_and_sms_logging_are_independent() -> None:
    calls_only = await started(FEATURES="call_logging")
    await calls_only.connect_company()
    await calls_only.services.reconciler.reconcile("c1")
    assert set(sub_kinds(calls_only)) == {"call"}
    assert body(await calls_only.webhook("c1", sms_event()))["deliveries"] == 0

    sms_only = await started(FEATURES="sms_logging")
    await sms_only.connect_company()
    await sms_only.services.reconciler.reconcile("c1")
    assert set(sub_kinds(sms_only)) == {"sms"}
    assert body(await sms_only.webhook("c1", call_event()))["deliveries"] == 0
    await sms_only.webhook("c1", sms_event(sms_id=9, text="hi"))
    await sms_only.drain()
    assert [r.reference for r in sms_only.connector.records.values()] == ["dialpad:c1:sms:9"]
    assert [c.KIND for c in sms_only.services.features.company_data] == ["SmsActivity"]  # type: ignore[union-attr]


async def test_feature_cannot_register_unknown_event_kind() -> None:
    STATE.kinds = ("voicemail_drop",)  # type: ignore[assignment]
    with pytest.raises(ConfigurationError, match="unknown event kind"):
        build_services(make_settings(FEATURES=ECHO))


async def test_session_reports_enabled_features() -> None:
    h = await started(FEATURES=f"call_logging,{ECHO}")
    await h.connect_company()
    r = await h.request("GET", "/session", headers={"X-Dialpad-Id-Token": h.id_token()})
    assert body(r)["features"] == ["platform", "call_logging", "echo"]


async def test_uninstall_deletes_feature_declared_data() -> None:
    h = await started(FEATURES="call_logging")
    await h.connect_company()
    await h.webhook("c1", call_event("88", "hangup"))
    await h.drain()
    assert await h.repo.get("CallActivity", ("c1", "88")) is not None
    await h.services.companies.uninstall("c1", "admin", h.services.features.company_data)  # type: ignore[union-attr]
    assert await h.repo.get("CallActivity", ("c1", "88")) is None


def test_echo_module_is_importable() -> None:
    assert echo_feature.feature.name == "echo"
