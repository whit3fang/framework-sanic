"""sms_logging: insert-once SMS metadata from Dialpad events. The message text is never stored."""

from __future__ import annotations

from typing import Any

from framework_sanic.core.domain.events import SmsEvent, contact_ref_from_party
from framework_sanic.core.domain.records import Record

from .models import SmsActivity


def sms_activity_from_event(ev: SmsEvent, *, company_id: str, ttl_seconds: float, now: float) -> SmsActivity:
    owner = ev.target.id if ev.target and ev.target.type == "user" else None
    created = ev.created_date or ev.event_timestamp
    base = (created / 1000.0) if created else now
    external_number = (
        ev.from_number if ev.direction == "inbound" else (ev.to_number[0] if ev.to_number else None)
    )
    return SmsActivity(
        company_id=company_id,
        sms_id=ev.id,
        direction=ev.direction,
        from_number=ev.from_number,
        to_numbers=list(ev.to_number),
        dialpad_contact_ref=contact_ref_from_party(ev.contact, external_number),
        target_type=ev.target.type if ev.target else None,
        target_id=ev.target.id if ev.target else None,
        owner_user_id=owner,
        created_date=created,
        message_status=ev.message_status,
        expires_at=base + ttl_seconds,
    )


def sms_merge_fn(ev: SmsEvent, *, company_id: str, ttl_seconds: float, now: float) -> Any:
    """Insert-once keyed by id. Skip if the first seen ``message_status`` is ``failed``."""

    def merge(current: Record | None) -> Record | None:
        if current is not None:
            return current
        if (ev.message_status or "").lower() == "failed":
            return None
        return sms_activity_from_event(
            ev, company_id=company_id, ttl_seconds=ttl_seconds, now=now
        ).to_record()

    return merge
