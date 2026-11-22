"""Dialpad webhook event models.

Parsing is defensive: unknown fields are dropped (which also guarantees that transcripts or
other unwanted fields never travel further than the accept path), ids are coerced to strings,
and shapes we have no real sample for (``operator``) are kept as raw dicts.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TERMINAL_CALL_STATES: frozenset[str] = frozenset({"hangup", "missed", "voicemail"})
GROUP_TARGET_TYPES: frozenset[str] = frozenset({"department", "callcenter"})


class Party(BaseModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)
    id: str | None = None
    type: str | None = None
    name: str | None = None
    phone: str | None = None
    email: str | None = None


class CallEvent(BaseModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)

    call_id: str
    state: str
    event_timestamp: int
    direction: str | None = None
    external_number: str | None = None
    internal_number: str | None = None
    contact: Party | None = None
    target: Party | None = None
    entry_point_target: Party | None = None
    entry_point_call_id: str | None = None
    master_call_id: str | None = None
    date_started: int | None = None
    date_rang: int | None = None
    date_connected: int | None = None
    date_ended: int | None = None
    duration: float | None = None
    recording_url: list[str] = Field(default_factory=list)
    voicemail_link: str | None = None
    # VERIFY: shape of the department-leg ``operator`` JSON. No real sample exists; kept raw.
    operator: dict[str, Any] | None = None

    @model_validator(mode="before")
    @classmethod
    def _collect_recordings(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        urls: list[str] = []
        raw = data.get("recording_url")
        if isinstance(raw, str):
            urls.append(raw)
        elif isinstance(raw, list):
            urls.extend(u for u in raw if isinstance(u, str))
        for detail in data.get("recording_details") or []:
            if isinstance(detail, dict) and isinstance(detail.get("url"), str):
                urls.append(detail["url"])
        data["recording_url"] = sorted(set(urls))
        for f in ("date_started", "date_rang", "date_connected", "date_ended", "event_timestamp"):
            if isinstance(data.get(f), str) and data[f].isdigit():
                data[f] = int(data[f])
        return data

    @field_validator("state")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.lower()

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_CALL_STATES

    @property
    def is_department_leg(self) -> bool:
        return bool(self.target and self.target.type in GROUP_TARGET_TYPES)

    @property
    def is_operator_leg(self) -> bool:
        """A user leg of a call that entered through a department/call center (VERIFY)."""
        return bool(
            self.target
            and self.target.type == "user"
            and self.entry_point_target
            and self.entry_point_target.type in GROUP_TARGET_TYPES
        )

    def operator_user_id(self) -> str | None:
        """Owner of a department leg, parsed defensively from ``operator`` (VERIFY shape)."""
        op = self.operator
        if not op:
            return None
        for path in (("id",), ("user_id",), ("target", "id"), ("operator", "id")):
            cur: Any = op
            for p in path:
                cur = cur.get(p) if isinstance(cur, dict) else None
            if cur is not None and not isinstance(cur, (dict, list)):
                return str(cur)
        return None


class SmsEvent(BaseModel):
    """SMS event. ``text`` is only present on the accept path and is encrypted before queueing."""

    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)

    id: int
    created_date: int | None = None
    event_timestamp: int | None = None
    direction: str | None = None
    from_number: str | None = None
    to_number: list[str] = Field(default_factory=list)
    contact: Party | None = None
    target: Party | None = None
    message_status: str | None = None
    text: str | None = None
    # VERIFY: whether group SMS (contact_group) should be logged; policy is open.
    contact_group: dict[str, Any] | None = None

    @field_validator("to_number", mode="before")
    @classmethod
    def _to_list(cls, v: Any) -> Any:
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return v

    @field_validator("id", mode="before")
    @classmethod
    def _int_id(cls, v: Any) -> Any:
        return int(v)


def classify_event(payload: dict[str, Any]) -> str | None:
    """Return "call", "sms" or None for an unrecognised payload."""
    if "call_id" in payload and "state" in payload:
        return "call"
    if "id" in payload and ("message_status" in payload or "from_number" in payload or "text" in payload):
        return "sms"
    return None


def contact_ref_from_party(party: Party | None, fallback_number: str | None) -> str | None:
    """Dialpad contact reference used to key ContactLink: contact id if known, else the number."""
    if party and party.id:
        return f"contact:{party.id}"
    number = (party.phone if party else None) or fallback_number
    return f"phone:{number}" if number else None
