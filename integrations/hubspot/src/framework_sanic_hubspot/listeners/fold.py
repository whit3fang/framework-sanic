"""Pure functions turning Dialpad call events into one ``LoggedCall``.

A call is a fold over its events ordered by ``(terminal, event_timestamp, state, digest)``:

* Each scalar field takes its value from the highest-ranked event that carries it. The rank of
  the winning event is stored per field, so the merge is a commutative, idempotent max: the
  result is identical for any order of arrival and for duplicated events.
* Terminal states (hangup, missed, voicemail) outrank every non-terminal state, so a later
  non-terminal event never regresses a terminal state.
* Additive fields (recording URLs) are a set union and merge at any time.
"""

from __future__ import annotations

from typing import Any

from framework_sanic.sdk import CallActivity, CallEvent, Record, stable_hash

from ..activities import LoggedCall

# Not part of an event's own fields.
_NOT_FROM_EVENT = {"schema_version", "company_id", "call_id", "recording_urls", "expires_at"}
# Excluded from the "has what HubSpot sees changed" hash.
_SYNC_FIELDS = {"contact", "synced_hash", "synced_recordings", "field_ranks"}


def event_rank(ev: CallEvent) -> list[Any]:
    digest = stable_hash(ev.model_dump(mode="json"), length=16)
    return [1 if ev.is_terminal else 0, ev.event_timestamp, ev.state, digest]


def fold_call_event(
    existing: LoggedCall | None, ev: CallEvent, *, company_id: str, ttl_seconds: float
) -> LoggedCall:
    act = (
        existing.model_copy(deep=True) if existing else LoggedCall(company_id=company_id, call_id=ev.call_id)
    )
    carried = CallActivity.from_event(ev, company_id=company_id).model_dump(exclude=_NOT_FROM_EVENT)
    rank = event_rank(ev)
    data = act.model_dump()
    ranks = dict(act.field_ranks)
    for field, value in carried.items():
        if value is None:
            continue
        current = ranks.get(field)
        if current is None or rank > current:
            data[field] = value
            ranks[field] = rank
    data["field_ranks"] = ranks
    data["recording_urls"] = sorted(set(act.recording_urls) | set(ev.recording_url))
    data["expires_at"] = max(act.expires_at or 0.0, ev.event_timestamp / 1000.0 + ttl_seconds)
    return LoggedCall.model_validate(data)


def call_merge_fn(ev: CallEvent, *, company_id: str, ttl_seconds: float) -> Any:
    def merge(current: Record | None) -> Record | None:
        existing = LoggedCall.from_record(current) if current else None
        return fold_call_event(existing, ev, company_id=company_id, ttl_seconds=ttl_seconds).to_record()

    return merge


def sync_view_hash(act: LoggedCall) -> str:
    """Hash of everything HubSpot sees; syncing is skipped when it is unchanged."""
    return stable_hash(act.model_dump(mode="json", exclude=_SYNC_FIELDS))
