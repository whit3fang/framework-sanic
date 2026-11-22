"""sms_logging data model. Owned by this feature, not by the framework core."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from framework_sanic.core.domain.entities import Entity, EntityRef, ExternalRef
from framework_sanic.core.domain.records import Key


class SmsActivity(Entity):
    """Insert-once SMS metadata. The message text is never stored."""

    KIND: ClassVar[str] = "SmsActivity"
    sms_id: int
    direction: str | None = None
    from_number: str | None = None
    to_numbers: list[str] = Field(default_factory=list)
    dialpad_contact_ref: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    owner_user_id: str | None = None
    created_date: int | None = None
    message_status: str | None = None
    expires_at: float | None = None
    synced: bool = False
    external_ref: ExternalRef | None = None
    external_contact_ref: EntityRef | None = None

    def key(self) -> Key:
        return (self.company_id, self.sms_id)

    @property
    def activity_key(self) -> str:
        return f"dialpad:{self.company_id}:sms:{self.sms_id}"


class SmsLog(BaseModel):
    """Connector-facing SMS payload: the stored metadata plus the transient decrypted text."""

    activity: SmsActivity
    text: str | None = None


ENTITIES: tuple[type[Entity], ...] = (SmsActivity,)
