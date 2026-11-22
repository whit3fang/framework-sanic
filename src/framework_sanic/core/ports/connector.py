"""Connector port: one implementation per external service (HubSpot, fake, ...).

The framework needs only what every integration shares: auth, the UI manifest and contact-like
entity lookup/creation, plus error mapping. What a connector *writes* in response to events
(call logs, SMS logs, ...) is defined by the features that use it: ``call_logging`` defines
``CallLogger`` and ``sms_logging`` defines ``SmsLogger`` (each in the feature's ``port.py``).
"""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from ..domain.entities import EntityRef
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
    ref: EntityRef
    properties: dict[str, Any] = Field(default_factory=dict)
    url: str | None = None


class NewEntity(BaseModel):
    object_type: str = "contact"
    properties: dict[str, Any]


ErrorKind = Literal["auth", "rate_limited", "transient", "permanent", "not_found"]


class ConnectorError(Exception):
    def __init__(self, kind: ErrorKind, message: str = "", retry_after: float | None = None) -> None:
        super().__init__(message or kind)
        self.kind: ErrorKind = kind
        self.retry_after = retry_after


class Connector(Protocol):
    name: str
    auth: ConnectorAuth
    capabilities: set[str]  # e.g. {"calls", "sms", "recordings", "notes", "create_contact"}

    def ui_manifest(self) -> UiManifest: ...

    async def search_entity(self, cred: ConnectorCredential, query: EntityQuery) -> list[Entity]: ...

    async def get_entity(self, cred: ConnectorCredential, ref: EntityRef) -> Entity: ...

    async def create_entity(self, cred: ConnectorCredential, data: NewEntity) -> Entity: ...

    def map_error(self, exc: Exception) -> ConnectorError: ...
