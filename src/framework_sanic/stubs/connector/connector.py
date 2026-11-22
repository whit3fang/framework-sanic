"""In-memory fake connector (a stub CRM) for tests, local development and the docker-compose harness.

Implements the ``Connector`` port: contacts (seeded in memory), matching by phone, auth and error
mapping. What happens to Dialpad activity is up to listeners (see ``stubs/listeners.py``).
"""

from __future__ import annotations

import asyncio
import itertools
import secrets
from typing import Literal
from urllib.parse import urlencode

from framework_sanic.core.domain.entities import EntityMap
from framework_sanic.core.ports.connector import (
    Caller,
    ConnectorAuth,
    ConnectorCredential,
    ConnectorError,
    ContactMatch,
    Entity,
    EntityQuery,
    ManifestField,
    NewEntity,
    UiManifest,
)
from framework_sanic.core.ports.platform import OAuthTokens
from framework_sanic.core.services.matching import match_by_phone
from framework_sanic.core.services.phone import digits


class FakeHTTPError(Exception):
    def __init__(self, status: int, retry_after: float | None = None) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.retry_after = retry_after


class FakeAuth:
    kind: Literal["oauth2", "api_key"] = "oauth2"
    scopes = ["contacts", "activities"]

    def authorize_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str:
        return f"{redirect_uri}?{urlencode({'code': 'fake-ext-' + secrets.token_hex(4), 'state': state})}"

    async def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> OAuthTokens:
        return OAuthTokens(
            access_token="ext-" + secrets.token_hex(8),
            refresh_token="ext-rt-" + secrets.token_hex(8),
            expires_in=3600,
            extra={"external_user_id": "ext-user-" + code[-4:]},
        )

    async def refresh(self, tokens: OAuthTokens) -> OAuthTokens:
        return OAuthTokens(
            access_token="ext-" + secrets.token_hex(8), refresh_token=tokens.refresh_token, expires_in=3600
        )


class FakeConnector:
    name = "fake"

    def __init__(self, *, latency: float = 0.0) -> None:
        self.auth: ConnectorAuth = FakeAuth()
        self.capabilities = {"calls", "sms", "recordings", "notes", "create_contact"}
        self.contacts: dict[str, Entity] = {}
        self.calls_made: list[str] = []
        self.fail_next: list[Exception] = []
        self.latency = latency
        self._ids = itertools.count(1)

    async def _op(self, name: str) -> None:
        self.calls_made.append(name)
        await asyncio.sleep(self.latency)
        if self.fail_next:
            raise self.fail_next.pop(0)

    def seed_contact(self, name: str, phone: str, email: str | None = None) -> Entity:
        cid = f"contact-{next(self._ids)}"
        entity = Entity(
            external_id=cid,
            external_type="contact",
            properties={"name": name, "phone": phone, "email": email},
            url=f"https://fake.example/contacts/{cid}",
        )
        self.contacts[cid] = entity
        return entity

    def ui_manifest(self) -> UiManifest:
        return UiManifest(
            connector=self.name,
            display_name="Fake CRM",
            fields=[
                ManifestField(key="name", label="Name"),
                ManifestField(key="email", label="Email", kind="email"),
                ManifestField(key="phone", label="Phone", kind="phone"),
            ],
            link_out_template="https://fake.example/contacts/{id}",
            create_fields=[
                ManifestField(key="name", label="Name"),
                ManifestField(key="email", label="Email", kind="email"),
                ManifestField(key="phone", label="Phone", kind="phone"),
            ],
        )

    async def search_entity(self, cred: ConnectorCredential, query: EntityQuery) -> list[Entity]:
        await self._op("search_entity")
        wanted = {digits(v) for v in query.phone_variants if digits(v)}
        out: list[Entity] = []
        for e in self.contacts.values():
            phone = digits(str(e.properties.get("phone") or ""))
            if wanted and phone and any(phone.endswith(w) or w.endswith(phone) for w in wanted):
                out.append(e)
            elif query.email and e.properties.get("email") == query.email:
                out.append(e)
            elif (
                query.text and not wanted and query.text.lower() in str(e.properties.get("name", "")).lower()
            ):
                out.append(e)
        return out[: query.limit]

    async def match_contact(self, cred: ConnectorCredential, caller: Caller) -> ContactMatch:
        return await match_by_phone(self, cred, caller)

    async def get_entity(self, cred: ConnectorCredential, entity_map: EntityMap) -> Entity:
        await self._op("get_entity")
        if entity_map.external_id not in self.contacts:
            raise ConnectorError("not_found", f"contact {entity_map.external_id} not found")
        return self.contacts[entity_map.external_id]

    async def create_entity(self, cred: ConnectorCredential, data: NewEntity) -> Entity:
        await self._op("create_entity")
        props = data.properties
        return self.seed_contact(
            str(props.get("name") or props.get("phone") or "Unknown"),
            str(props.get("phone") or ""),
            props.get("email"),
        )

    def map_error(self, exc: Exception) -> ConnectorError:
        if isinstance(exc, ConnectorError):
            return exc
        if isinstance(exc, FakeHTTPError):
            if exc.status == 401:
                return ConnectorError("auth", str(exc))
            if exc.status == 429:
                return ConnectorError("rate_limited", str(exc), retry_after=exc.retry_after)
            if exc.status >= 500:
                return ConnectorError("transient", str(exc))
            if exc.status == 404:
                return ConnectorError("not_found", str(exc))
            return ConnectorError("permanent", str(exc))
        if isinstance(exc, (TimeoutError, ConnectionError)):
            return ConnectorError("transient", str(exc))
        return ConnectorError("transient", repr(exc))
