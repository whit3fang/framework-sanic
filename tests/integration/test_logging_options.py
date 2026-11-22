"""call_logging / sms_logging options (CALL_LOGGING_*, SMS_LOGGING_*) and connector filter hooks."""

from __future__ import annotations

from typing import Any

from framework_sanic.bootstrap import startup
from framework_sanic.sdk.call_logging import CallActivity, CallLogFilter
from framework_sanic.sdk.sms_logging import SmsActivity, SmsLogFilter
from framework_sanic.stubs.connector.connector import FakeConnector
from framework_sanic.testing import Harness, build_harness, call_event, sms_event


async def run(connector: Any = None, **settings: Any) -> Harness:
    h = build_harness(connector=connector, **settings)
    await startup(h.services, schedules=False)
    await h.connect_company()
    return h


async def call(h: Harness, call_id: str, state: str = "hangup", **kw: Any) -> None:
    assert (await h.webhook("c1", call_event(call_id, state, **kw))).status == 200
    await h.drain()


def logged_calls(h: Harness) -> set[str]:
    return {r.reference.rsplit(":", 1)[1] for r in h.connector.records_for("call") if r.reference}


async def test_defaults_log_everything() -> None:
    h = await run()
    await call(h, "1")
    await call(h, "2", "missed")
    await call(h, "3", direction="outbound")
    assert logged_calls(h) == {"1", "2", "3"}


async def test_call_filters_from_settings() -> None:
    h = await run(
        CALL_LOGGING_DIRECTIONS="inbound",
        CALL_LOGGING_SKIP_STATES="missed",
        CALL_LOGGING_MIN_DURATION_SECONDS="90",
    )
    await call(h, "1", duration=120_000.0)  # logged
    await call(h, "2", "missed")  # skipped state
    await call(h, "3", direction="outbound", duration=120_000.0)  # direction not logged
    await call(h, "4")  # 60 s < 90 s
    assert logged_calls(h) == {"1"}
    await call(h, "2", "missed")  # a redelivery stays skipped and costs no connector call
    assert logged_calls(h) == {"1"}


async def test_recordings_can_be_turned_off() -> None:
    h = await run(CALL_LOGGING_RECORDINGS="false")
    await call(h, "1")
    await call(h, "1", "recording", recording_details=[{"url": "https://dialpad.example/rec/1"}])
    [rec] = h.connector.records_for("call")
    assert rec.recordings == [] and "attach_recording" not in h.connector.calls_made


class OnlyLongCalls(FakeConnector):
    def should_log_call(self, activity: CallActivity) -> bool:
        return (activity.duration_ms or 0) >= 100_000


class NoShortCodes(FakeConnector):
    def should_log_sms(self, activity: SmsActivity) -> bool:
        return len(activity.from_number or "") > 6


async def test_connector_call_hook() -> None:
    assert isinstance(OnlyLongCalls(), CallLogFilter)
    h = await run(connector=OnlyLongCalls())
    await call(h, "1", duration=150_000.0)
    await call(h, "2")  # 60 s
    assert logged_calls(h) == {"1"}


async def sms(h: Harness, sms_id: int, **kw: Any) -> None:
    assert (await h.webhook("c1", sms_event(sms_id, **kw))).status == 200
    await h.drain()


async def test_sms_options_and_hook() -> None:
    h = await run(SMS_LOGGING_DIRECTIONS="inbound", SMS_LOGGING_INCLUDE_TEXT="false")
    await sms(h, 1, text="secret text")
    await sms(h, 2, direction="outbound")
    [rec] = h.connector.records_for("sms")
    assert rec.reference == "dialpad:c1:sms:1" and rec.properties["text"] is None

    assert isinstance(NoShortCodes(), SmsLogFilter)
    h = await run(connector=NoShortCodes())
    await sms(h, 3, from_number="12345")
    await sms(h, 4)
    assert [r.reference for r in h.connector.records_for("sms")] == ["dialpad:c1:sms:4"]
