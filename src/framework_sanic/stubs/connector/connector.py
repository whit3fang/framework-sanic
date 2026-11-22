"""In-memory fake connector (a stub CRM) for tests, local development and the docker-compose harness.

Implements the base Connector plus the CallLogger (call_logging) and SmsLogger (sms_logging) ports. It deliberately does not
deduplicate on its own: call_logging stage 2 must use ``find_by_reference`` so that
duplicate deliveries never create a second external record.
"""

from __future__ import annotations

import asyncio
import itertools
import secrets
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlencode

from framework_sanic.core.domain.entities import EntityRef, ExternalRef
from framework_sanic.core.ports.connector import (
    ConnectorAuth,
    ConnectorCredential,
    ConnectorError,
    Entity,
    EntityQuery,
    ManifestField,
    NewEntity,
    UiManifest,
)
from framework_sanic.core.ports.platform import OAuthTokens
from framework_sanic.core.services.phone import digits
from framework_sanic.features.call_logging.models import CallActivity, Note
from framework_sanic.features.sms_logging.models import SmsLog


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


@dataclass
class FakeRecord:
    ref: ExternalRef
    properties: dict[str, Any]
    reference: str | None = None
    recordings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class FakeConnector:
    name = "fake"

    def __init__(self, *, latency: float = 0.0) -> None:
        self.auth: ConnectorAuth = FakeAuth()
        self.capabilities = {"calls", "sms", "recordings", "notes", "create_contact"}
        self.contacts: dict[str, Entity] = {}
        self.records: dict[str, FakeRecord] = {}  # id -> record (calls, sms)
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
            ref=EntityRef(object_type="contact", id=cid),
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

    async def get_entity(self, cred: ConnectorCredential, ref: EntityRef) -> Entity:
        await self._op("get_entity")
        if ref.id not in self.contacts:
            raise ConnectorError("not_found", f"contact {ref.id} not found")
        return self.contacts[ref.id]

    async def create_entity(self, cred: ConnectorCredential, data: NewEntity) -> Entity:
        await self._op("create_entity")
        props = data.properties
        return self.seed_contact(
            str(props.get("name") or props.get("phone") or "Unknown"),
            str(props.get("phone") or ""),
            props.get("email"),
        )

    def _create(self, object_type: str, reference: str, props: dict[str, Any]) -> ExternalRef:
        ref = ExternalRef(system=self.name, object_type=object_type, id=f"{object_type}-{next(self._ids)}")
        self.records[ref.id] = FakeRecord(ref=ref, properties=props, reference=reference)
        return ref

    async def upsert_call(self, cred: ConnectorCredential, activity: CallActivity) -> ExternalRef:
        await self._op("upsert_call")
        props = {
            "state": activity.state,
            "direction": activity.direction,
            "duration_ms": activity.duration_ms,
            "external_number": activity.external_number,
            "owner": activity.owner_user_id,
            "contact": activity.external_contact_ref.id if activity.external_contact_ref else None,
            "recordings": list(activity.recording_urls),
        }
        if activity.external_ref and activity.external_ref.id in self.records:
            self.records[activity.external_ref.id].properties.update(props)
            return activity.external_ref
        return self._create("call", activity.activity_key, props)

    async def log_sms(self, cred: ConnectorCredential, activity: SmsLog) -> ExternalRef:
        await self._op("log_sms")
        act = activity.activity
        return self._create(
            "sms",
            act.activity_key,
            {
                "direction": act.direction,
                "text": activity.text,
                "contact": act.external_contact_ref.id if act.external_contact_ref else None,
            },
        )

    async def attach_recording(self, cred: ConnectorCredential, ref: ExternalRef, url: str) -> None:
        await self._op("attach_recording")
        rec = self.records[ref.id]
        if url not in rec.recordings:
            rec.recordings.append(url)

    async def add_note(self, cred: ConnectorCredential, ref: ExternalRef, note: Note) -> None:
        await self._op("add_note")
        rec = self.records[ref.id]
        marker = f"{note.note_id}:{note.body}"
        if marker not in rec.notes:
            rec.notes.append(marker)

    async def find_by_reference(self, cred: ConnectorCredential, activity_key: str) -> ExternalRef | None:
        await self._op("find_by_reference")
        for rec in self.records.values():
            if rec.reference == activity_key:
                return rec.ref
        return None

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

    # test helpers
    def records_for(self, object_type: str) -> list[FakeRecord]:
        return [r for r in self.records.values() if r.ref.object_type == object_type]
