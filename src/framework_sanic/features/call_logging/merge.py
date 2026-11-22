"""call_logging: pure fold functions turning Dialpad events into activity records.

Call activity is a fold over events ordered by ``(terminal, event_timestamp, state, digest)``:

* Each scalar field takes its value from the highest-ranked event that carries it. The rank of
  the winning event is stored per field, so the merge is a commutative, idempotent max: the
  result is identical for any order of arrival and for duplicated events.
* Terminal states (hangup, missed, voicemail) outrank every non-terminal state, so a later
  non-terminal event never regresses a terminal state.
* Additive fields (recording URLs) are a set union and merge at any time.

Correctness never depends on the Redis dedupe fast path; the repository's atomic upsert applies
these functions.
"""

from __future__ import annotations

from typing import Any

from framework_sanic.core.domain.events import CallEvent, contact_ref_from_party
from framework_sanic.core.domain.records import Record, stable_hash

from .models import CallActivity

# Fields that are excluded from the "has the external view changed" hash.
_SYNC_FIELDS = {"external_ref", "external_contact_ref", "synced_hash", "synced_recordings", "field_ranks"}


def event_rank(ev: CallEvent) -> list[Any]:
    digest = stable_hash(ev.model_dump(mode="json"), length=16)
    return [1 if ev.is_terminal else 0, ev.event_timestamp, ev.state, digest]


def _event_fields(ev: CallEvent) -> dict[str, Any]:
    """Scalar activity fields carried by this event (None = not carried)."""
    if ev.is_department_leg:
        owner = ev.operator_user_id()
    elif ev.target and ev.target.type == "user":
        owner = ev.target.id
    else:
        owner = None
    return {
        "state": ev.state,
        "terminal": ev.is_terminal,
        "direction": ev.direction,
        "external_number": ev.external_number,
        "internal_number": ev.internal_number,
        "dialpad_contact_ref": contact_ref_from_party(ev.contact, ev.external_number),
        "contact_name": ev.contact.name if ev.contact else None,
        "target_type": ev.target.type if ev.target else None,
        "target_id": ev.target.id if ev.target else None,
        "entry_point_target_type": ev.entry_point_target.type if ev.entry_point_target else None,
        "entry_point_target_id": ev.entry_point_target.id if ev.entry_point_target else None,
        "is_department_leg": ev.is_department_leg if ev.target else None,
        "owner_user_id": owner,
        "started_at": ev.date_started,
        "rang_at": ev.date_rang,
        "connected_at": ev.date_connected,
        "ended_at": ev.date_ended,
        "duration_ms": ev.duration,
        "voicemail_url": ev.voicemail_link,
    }


def fold_call_event(
    existing: CallActivity | None, ev: CallEvent, *, company_id: str, ttl_seconds: float
) -> CallActivity:
    act = (
        existing.model_copy(deep=True)
        if existing
        else CallActivity(company_id=company_id, call_id=ev.call_id)
    )
    rank = event_rank(ev)
    data = act.model_dump()
    ranks = dict(act.field_ranks)
    for field, value in _event_fields(ev).items():
        if value is None:
            continue
        current = ranks.get(field)
        if current is None or rank > current:
            data[field] = value
            ranks[field] = rank
    data["field_ranks"] = ranks
    data["recording_urls"] = sorted(set(act.recording_urls) | set(ev.recording_url))
    expires = ev.event_timestamp / 1000.0 + ttl_seconds
    data["expires_at"] = max(act.expires_at or 0.0, expires)
    return CallActivity.model_validate(data)


def call_merge_fn(ev: CallEvent, *, company_id: str, ttl_seconds: float) -> Any:
    def merge(current: Record | None) -> Record | None:
        existing = CallActivity.from_record(current) if current else None
        return fold_call_event(existing, ev, company_id=company_id, ttl_seconds=ttl_seconds).to_record()

    return merge


def sync_view_hash(act: CallActivity) -> str:
    """Hash of everything the external service sees; stage 2 skips work when unchanged."""
    return stable_hash(act.model_dump(mode="json", exclude=_SYNC_FIELDS))
