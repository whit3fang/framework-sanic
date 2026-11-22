"""Dialpad → HubSpot translation. Pure functions (no I/O), so each mapping is easy to read and test.

Every activity record carries the Dialpad activity key in ``REFERENCE_PROPERTY``; see
``references.py``.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from framework_sanic.sdk import ConnectorCredential, Entity
from framework_sanic.sdk.call_logging import CallActivity, Note
from framework_sanic.sdk.sms_logging import SmsLog

from .crm import HubSpotObject

REFERENCE_PROPERTY = "dialpad_activity_key"
CONTACT_PROPERTIES = ["firstname", "lastname", "email", "phone", "mobilephone", "company", "jobtitle"]
CALL_STATUS = {"hangup": "COMPLETED", "missed": "NO_ANSWER", "voicemail": "NO_ANSWER"}


def iso_timestamp(ms: int | float | None) -> str:
    """HubSpot ``hs_timestamp`` (ISO 8601, UTC) from epoch milliseconds; now when missing."""
    ts = (ms / 1000.0) if ms else dt.datetime.now(dt.UTC).timestamp()
    return dt.datetime.fromtimestamp(ts, tz=dt.UTC).isoformat().replace("+00:00", "Z")


def duration_text(ms: float | None) -> str:
    if not ms:
        return ""
    s = int(ms // 1000)
    return f"{s // 60}m {s % 60}s"


# --- contacts --------------------------------------------------------------------------------
def portal_id(cred: ConnectorCredential, default: str = "") -> str:
    """The HubSpot portal of this credential (stored at OAuth time), else the configured default."""
    return str(cred.tokens.extra.get("portal_id") or default or "")


def contact_entity(obj: HubSpotObject, portal: str) -> Entity:
    """A HubSpot contact as the framework's ``Entity`` (adds ``name``, links to the record)."""
    props = obj.get("properties") or {}
    name = " ".join(p for p in (props.get("firstname"), props.get("lastname")) if p)
    props = {**props, "name": name or props.get("email") or props.get("phone") or ""}
    url = f"https://app.hubspot.com/contacts/{portal}/record/0-1/{obj['id']}" if portal else None
    return Entity(external_id=str(obj["id"]), external_type="contact", properties=props, url=url)


def new_contact_properties(properties: dict[str, Any]) -> dict[str, Any]:
    """Drop empty values: HubSpot rejects empty strings for some property types."""
    return {k: v for k, v in properties.items() if v not in (None, "")}


# --- calls -----------------------------------------------------------------------------------
def call_properties(act: CallActivity) -> dict[str, Any]:
    inbound = act.direction == "inbound"
    title = f"{'Inbound' if inbound else 'Outbound'} Dialpad call"
    if act.state in ("missed", "voicemail"):
        title += f" ({act.state})"
    props: dict[str, Any] = {
        "hs_timestamp": iso_timestamp(act.started_at or act.ended_at),
        "hs_call_title": title,
        "hs_call_body": f"{title} {duration_text(act.duration_ms)}".strip(),
        "hs_call_direction": "INBOUND" if inbound else "OUTBOUND",
        "hs_call_status": CALL_STATUS.get(act.state or "", "COMPLETED"),
        "hs_call_duration": int(act.duration_ms or 0),
        "hs_call_from_number": act.external_number if inbound else act.internal_number,
        "hs_call_to_number": act.internal_number if inbound else act.external_number,
        REFERENCE_PROPERTY: act.activity_key,
    }
    recording = act.recording_urls[0] if act.recording_urls else act.voicemail_url
    if recording:
        props["hs_call_recording_url"] = recording  # linked, never copied
    return props


def recording_properties(url: str) -> dict[str, Any]:
    """HubSpot holds one recording URL per call."""
    return {"hs_call_recording_url": url}


# --- notes -----------------------------------------------------------------------------------
def note_properties(note: Note) -> dict[str, Any]:
    return {
        "hs_note_body": note.body,
        "hs_timestamp": iso_timestamp(note.created_at * 1000),
        REFERENCE_PROPERTY: note.activity_key,
    }


# --- SMS -------------------------------------------------------------------------------------
def sms_properties(sms: SmsLog) -> dict[str, Any]:
    """VERIFY: HubSpot object used for SMS (a communication with channel type SMS is assumed).
    ``sms.text`` is decrypted for this call only: it goes to HubSpot and nowhere else."""
    return {
        "hs_communication_channel_type": "SMS",
        "hs_communication_logged_from": "CRM",
        "hs_communication_body": sms.text or "",
        "hs_timestamp": iso_timestamp(sms.activity.created_date),
        REFERENCE_PROPERTY: sms.activity.activity_key,
    }
