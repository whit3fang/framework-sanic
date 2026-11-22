"""Persistent entities. Every entity has a deterministic key and a ``schema_version``.

Entities are converted to plain JSON-compatible records (``to_record``) before they reach a
Repository, so storage adapters never need to know about these classes.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field

from .records import Key, Record


class Entity(BaseModel):
    model_config = ConfigDict(extra="ignore")

    KIND: ClassVar[str]
    SCHEMA_VERSION: ClassVar[int] = 1

    schema_version: int = 1
    company_id: str

    def key(self) -> Key:
        raise NotImplementedError

    def to_record(self) -> Record:
        data = self.model_dump(mode="json")
        data["schema_version"] = self.SCHEMA_VERSION
        return data

    @classmethod
    def from_record(cls, record: Record) -> Self:
        return cls.model_validate(record)


class Company(Entity):
    KIND: ClassVar[str] = "Company"
    name: str = ""
    installed_at: float = 0.0
    webhook_endpoint_id: str | None = None
    # Dialpad connection health. "invalid" parks stage-2 sync for the company.
    connection_state: Literal["ok", "invalid", "uninstalled"] = "ok"
    parked_reason: str | None = None

    def key(self) -> Key:
        return (self.company_id,)


class DialpadCredential(Entity):
    KIND: ClassVar[str] = "DialpadCredential"
    credential_id: str
    admin_user_id: str
    priority: float  # lower = primary; ordered primary then fallback
    access_token_enc: str
    refresh_token_enc: str | None = None
    token_expires_at: float | None = None  # NOT `expires_at`: that name is the record TTL
    scopes: list[str] = Field(default_factory=list)
    status: Literal["ok", "bad"] = "ok"
    version: int = 1  # compare-and-set on refresh

    def key(self) -> Key:
        return (self.company_id, self.credential_id)


class ExternalCredential(Entity):
    KIND: ClassVar[str] = "ExternalCredential"
    scope: Literal["user", "company"]
    user_id: str = ""  # empty for company scope
    connector: str
    tokens_enc: str  # encrypted JSON blob (access/refresh tokens or API key)
    token_expires_at: float | None = None  # NOT `expires_at`: that name is the record TTL
    status: Literal["ok", "bad"] = "ok"
    version: int = 1
    external_user_id: str | None = None

    def key(self) -> Key:
        return (self.company_id, self.scope, self.user_id)


class User(Entity):
    KIND: ClassVar[str] = "User"
    dialpad_user_id: str
    name: str = ""
    email: str | None = None
    office_id: str | None = None
    external_user_id: str | None = None
    is_admin: bool = False  # VERIFY: presence/semantics of is_admin on the Dialpad user object

    def key(self) -> Key:
        return (self.company_id, self.dialpad_user_id)


TargetType = Literal["company", "office", "user", "department", "callcenter"]


class Target(Entity):
    KIND: ClassVar[str] = "Target"
    target_type: TargetType
    target_id: str
    name: str = ""
    parent_type: TargetType | None = None
    parent_id: str | None = None
    override: bool | None = None  # None = inherit from parent
    effective_enabled: bool = True  # denormalized so queries do not walk the hierarchy
    deleted: bool = False

    def key(self) -> Key:
        return (self.company_id, self.target_type, self.target_id)


class Subscription(Entity):
    KIND: ClassVar[str] = "Subscription"
    target_type: TargetType
    target_id: str
    kind: Literal["call", "sms"]
    dialpad_subscription_id: str | None = None
    enabled: bool = True
    status: Literal["creating", "active"] = "creating"
    claimed_at: float = 0.0

    def key(self) -> Key:
        return (self.company_id, self.target_type, self.target_id, self.kind)


class EntityMap(Entity):
    """A Dialpad object and the external record that stands for it, one per Dialpad id.

    ``dialpad_id`` is either a Dialpad contact (``contact:<id>`` or ``phone:<number>``) mapped to a
    CRM contact, or an activity key (``dialpad:<company>:call|sms|note:<id>``) mapped to the
    record the integration logged for it. ``external_type`` is whatever the integration uses
    ("contact", "lead", "calls", "communications", ...).

    Contact maps are written by matching (automatic or rematch, treated the same) and stay until
    the next rematch. Activities keep a copy of the contact map they were logged with, so a
    rematch only affects future activities. Connectors build maps without ``company_id``; the
    framework fills it when storing.
    """

    KIND: ClassVar[str] = "EntityMap"
    company_id: str = ""
    dialpad_id: str
    external_id: str
    external_type: str
    updated_at: float = 0.0

    def key(self) -> Key:
        return (self.company_id, self.dialpad_id)


class OutboxEntry(Entity):
    """Written in the same UnitOfWork as a state change; drives a follow-up job (see services.outbox)."""

    KIND: ClassVar[str] = "Outbox"
    outbox_id: str
    topic: str
    lane: str
    job: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: float

    def key(self) -> Key:
        return (self.company_id, self.outbox_id)


class AuditEvent(Entity):
    KIND: ClassVar[str] = "AuditEvent"
    event_id: str
    action: Literal["connect", "disconnect", "config_change", "delete", "external_connect"]
    actor: str
    details: dict[str, Any] = Field(default_factory=dict)
    at: float

    def key(self) -> Key:
        return (self.company_id, self.event_id)


ALL_ENTITIES: tuple[type[Entity], ...] = (
    Company,
    DialpadCredential,
    ExternalCredential,
    User,
    Target,
    Subscription,
    EntityMap,
    OutboxEntry,
    AuditEvent,
)

# Framework kinds deleted on uninstall (the audit log is kept). Features add their own kinds with
# ``ctx.declare_company_data``.
COMPANY_DATA_KINDS: tuple[str, ...] = tuple(e.KIND for e in ALL_ENTITIES if e is not AuditEvent)
