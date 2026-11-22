"""Sample HubSpot logging records for connector and mapping tests."""

from __future__ import annotations

from typing import Any

from framework_sanic_hubspot.activities import CallNote, LoggedCall, LoggedSms

PHONE = "+14155550100"


def sample_call(call_id: str = "9", **kw: Any) -> LoggedCall:
    """A terminal inbound call as ``hubspot.calls`` hands it to ``upsert_call``."""
    fields: dict[str, Any] = {
        "company_id": "c1",
        "call_id": call_id,
        "state": "hangup",
        "terminal": True,
        "direction": "inbound",
        "external_number": PHONE,
        "internal_number": "+14155550199",
        "duration_ms": 61_000,
        "started_at": 1_700_000_000_000,
    }
    return LoggedCall(**{**fields, **kw})


def sample_note(note_id: str = "note-00001", call_id: str = "9") -> CallNote:
    return CallNote(
        company_id="c1", note_id=note_id, call_id=call_id, author_user_id="u", body="hi", created_at=1.0
    )


def sample_sms(sms_id: int = 77, **kw: Any) -> LoggedSms:
    fields: dict[str, Any] = {
        "company_id": "c1",
        "sms_id": sms_id,
        "direction": "inbound",
        "from_number": PHONE,
    }
    return LoggedSms(**{**fields, **kw})
