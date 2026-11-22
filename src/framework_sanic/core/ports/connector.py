"""Connector port: one implementation per external service (HubSpot, fake, ...).

The framework needs only what every integration shares: auth, the UI manifest, contact matching
and lookup/creation, plus error mapping. What is written in response to Dialpad activity (call
logs, SMS logs, notes, ...) is up to the integration's listeners, which call whatever methods
their own connector offers.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from ..domain.entities import EntityMap
from ..ports.platform import OAuthTokens


class ConnectorCredential(BaseModel):
    """Decrypted credential handed to a connector for one call."""

    scope: Literal["user", "company"]
    user_id: str = ""
    tokens: OAuthTokens
    external_user_id: str | None = None


class ConnectorAuth(Protocol):
    kind: Literal["oauth2", "api_key"]
    scopes: list[str]

    def authorize_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str: ...

    async def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> OAuthTokens: ...

    async def refresh(self, tokens: OAuthTokens) -> OAuthTokens: ...


class ManifestField(BaseModel):
    key: str
    label: str
    kind: Literal["text", "email", "phone", "url", "date"] = "text"


class UiManifest(BaseModel):
    """Drives the manifest-based contact card; the UI has no connector-specific code."""

    connector: str
    display_name: str
    object_label: str = "Contact"
    fields: list[ManifestField]
    link_out_template: str | None = None  # e.g. "https://app.hubspot.com/contacts/{portal}/record/0-1/{id}"
    search_placeholder: str = "Search by name, email or phone"
    create_fields: list[ManifestField] = Field(default_factory=list)


class EntityQuery(BaseModel):
    phone_variants: list[str] = Field(default_factory=list)
    email: str | None = None
    text: str | None = None
    limit: int = 10


class Entity(BaseModel):
    external_id: str
    external_type: str  # the integration's own name, e.g. "contact" or "lead"
    properties: dict[str, Any] = Field(default_factory=dict)
    url: str | None = None


class Caller(BaseModel):
    """The other party of a Dialpad call or SMS, to be matched to a CRM contact."""

    dialpad_ref: str | None = None  # "contact:<dialpad contact id>" or "phone:<number>"
    number: str | None = None  # E.164 when it parses, else as Dialpad sent it
    # "card": the contact card is open (candidates are shown to the user).
    # "activity": a call/SMS is being logged (the match is associated with the record).
    purpose: Literal["card", "activity"] = "card"

    @property
    def dialpad_contact_id(self) -> str | None:
        ref = self.dialpad_ref or ""
        return ref.split(":", 1)[1] if ref.startswith("contact:") else None


class ContactMatch(BaseModel):
    entity: Entity | None = None  # the chosen contact; None = no match
    candidates: list[Entity] = Field(default_factory=list)  # alternatives offered for rematch


class NewEntity(BaseModel):
    object_type: str = "contact"
    properties: dict[str, Any]


ErrorKind = Literal["auth", "rate_limited", "transient", "permanent", "not_found"]


class ConnectorError(Exception):
    def __init__(self, kind: ErrorKind, message: str = "", retry_after: float | None = None) -> None:
        super().__init__(message or kind)
        self.kind: ErrorKind = kind
        self.retry_after = retry_after


@runtime_checkable
class Connector(Protocol):
    name: str
    auth: ConnectorAuth
    capabilities: set[str]  # e.g. {"calls", "sms", "recordings", "notes", "create_contact"}

    def ui_manifest(self) -> UiManifest: ...

    async def search_entity(self, cred: ConnectorCredential, query: EntityQuery) -> list[Entity]: ...

    async def get_entity(self, cred: ConnectorCredential, entity_map: EntityMap) -> Entity:
        """The current CRM record behind a stored contact map; ``not_found`` if it was deleted."""
        ...

    async def create_entity(self, cred: ConnectorCredential, data: NewEntity) -> Entity: ...

    async def match_contact(self, cred: ConnectorCredential, caller: Caller) -> ContactMatch:
        """Which CRM contact is this caller? The integration's matching rules (fields, formats,
        ambiguity handling). Only called while the caller has no stored ``EntityMap``; the match
        found is then stored until a rematch replaces it. ``framework_sanic.sdk.match_by_phone`` is a simple default."""
        ...

    def map_error(self, exc: Exception) -> ConnectorError: ...
