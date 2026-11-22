"""Property tests for the HubSpot call fold (listeners/fold.py).

For generated call sequences, shuffling, duplicating and dropping non-terminal progress events
must yield an identical final activity. The terminal event carries the full call snapshot (as
Dialpad's do); additive events (recordings) carry data no other event has, so they are never
dropped.
"""

from __future__ import annotations

import random
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from framework_sanic.sdk import CallEvent
from framework_sanic_hubspot.activities import LoggedCall as CallActivity
from framework_sanic_hubspot.listeners.fold import fold_call_event

TTL = 90 * 86400.0
TERMINAL_CALL_STATES = frozenset({"hangup", "missed", "voicemail"})
PROGRESS = ["calling", "ringing", "connected", "hold", "unhold", "preanswer"]


@st.composite
def call_sequences(draw: st.DrawFn) -> list[CallEvent]:
    call_id = str(draw(st.integers(1, 10**9)))
    t0 = draw(st.integers(1_600_000_000_000, 1_800_000_000_000))
    n_progress = draw(st.integers(0, 6))
    terminal = draw(st.sampled_from(sorted(TERMINAL_CALL_STATES)))
    direction = draw(st.sampled_from(["inbound", "outbound"]))
    number = draw(st.from_regex(r"\+1415555[0-9]{4}", fullmatch=True))
    owner = draw(st.sampled_from(["u1", "u2"]))
    target = {"id": owner, "type": "user"}
    events: list[dict[str, Any]] = []
    t = t0
    for _ in range(n_progress):
        t += draw(st.integers(1, 5000))
        events.append(
            {
                "call_id": call_id,
                "state": draw(st.sampled_from(PROGRESS)),
                "event_timestamp": t,
                "direction": direction,
                "external_number": number,
                "target": target,
                "date_started": t0,
                "duration": draw(st.floats(0, 1000)),  # partial data, superseded later
            }
        )
    t += draw(st.integers(1, 5000))
    terminal_ts = t
    events.append(
        {
            "call_id": call_id,
            "state": terminal,
            "event_timestamp": terminal_ts,
            "direction": direction,
            "external_number": number,
            "internal_number": "+14155550199",
            "target": target,
            "contact": {"id": "77", "name": "Ada"},
            "date_started": t0,
            "date_ended": terminal_ts,
            "duration": float(terminal_ts - t0),
            "voicemail_link": "https://vm" if terminal == "voicemail" else None,
        }
    )
    for i in range(draw(st.integers(0, 2))):  # additive recording events, possibly after hangup
        t += draw(st.integers(1, 5000))
        events.append(
            {
                "call_id": call_id,
                "state": "recording",
                "event_timestamp": t,
                "recording_url": [f"https://rec/{call_id}/{i}"],
                "target": target,
            }
        )
    return [CallEvent.model_validate(e) for e in events]


def fold(events: list[CallEvent]) -> CallActivity | None:
    act: CallActivity | None = None
    for ev in events:
        # round-trip through the storage representation like the repository does
        act = fold_call_event(act, ev, company_id="c1", ttl_seconds=TTL)
        act = CallActivity.from_record(act.to_record())
    return act


def dump(act: CallActivity | None) -> dict[str, Any]:
    assert act is not None
    return act.model_dump(mode="json")


@settings(max_examples=300, deadline=None)
@given(call_sequences(), st.randoms(use_true_random=False))
def test_shuffle_duplicate_drop_converge(events: list[CallEvent], rnd: random.Random) -> None:
    expected = dump(fold(events))
    variant = list(events)
    # drop a random subset of non-terminal progress events
    variant = [e for e in variant if e.is_terminal or e.state == "recording" or rnd.random() < 0.5]
    # duplicate some events
    variant += [rnd.choice(variant) for _ in range(rnd.randint(0, 4))]
    rnd.shuffle(variant)
    assert dump(fold(variant)) == expected


@settings(max_examples=200, deadline=None)
@given(call_sequences(), st.randoms(use_true_random=False))
def test_terminal_state_never_regresses(events: list[CallEvent], rnd: random.Random) -> None:
    order = list(events)
    rnd.shuffle(order)
    act: CallActivity | None = None
    seen_terminal = False
    for ev in order:
        act = fold_call_event(act, ev, company_id="c1", ttl_seconds=TTL)
        seen_terminal |= ev.is_terminal
        if seen_terminal:
            assert act.terminal and act.state in TERMINAL_CALL_STATES


@settings(max_examples=100, deadline=None)
@given(call_sequences())
def test_fold_is_idempotent(events: list[CallEvent]) -> None:
    act = fold(events)
    for ev in events:
        assert fold_call_event(act, ev, company_id="c1", ttl_seconds=TTL).to_record() == act.to_record()  # type: ignore[union-attr]


def test_recordings_merge_after_terminal() -> None:
    hang = CallEvent.model_validate(
        {"call_id": "1", "state": "hangup", "event_timestamp": 10, "duration": 5.0}
    )
    rec = CallEvent.model_validate(
        {
            "call_id": "1",
            "state": "recording",
            "event_timestamp": 20,
            "recording_details": [{"url": "https://r/1"}],
        }
    )
    act = fold([hang, rec])
    assert act is not None and act.state == "hangup" and act.recording_urls == ["https://r/1"]
    assert act.expires_at == 20 / 1000 + TTL


def test_transcripts_and_unknown_fields_are_dropped() -> None:
    ev = CallEvent.model_validate(
        {
            "call_id": 5,
            "state": "hangup",
            "event_timestamp": 1,
            "transcription_text": "secret",
            "custom_data": {"x": 1},
        }
    )
    assert "transcription_text" not in ev.model_dump() and ev.call_id == "5"
