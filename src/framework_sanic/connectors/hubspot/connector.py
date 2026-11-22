"""HubSpot connector (CRM v3 objects + v4 default associations).

Idempotency: every call / communication / note carries the Dialpad activity key in a custom
property (``REFERENCE_PROPERTY``); ``find_by_reference`` searches it before creating.

VERIFY items (see docs/open-questions.md):
* HubSpot object used for SMS activity (communications with channel type SMS is assumed).
* OAuth scopes required for calls / communications / notes.
* PKCE support on the HubSpot authorize endpoint (challenge is sent; ignored if unsupported).
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal
from urllib.parse import urlencode

import httpx

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
from framework_sanic.features.call_logging.models import CallActivity, Note
from framework_sanic.features.sms_logging.models import SmsLog

REFERENCE_PROPERTY = "dialpad_activity_key"
CONTACT_PROPERTIES = ["firstname", "lastname", "email", "phone", "mobilephone", "company", "jobtitle"]
OBJECT_FOR_KEY = {"call": "calls", "sms": "communications", "note": "notes"}
CALL_STATUS = {"hangup": "COMPLETED", "missed": "NO_ANSWER", "voicemail": "NO_ANSWER"}


def _iso(ms: int | float | None) -> str:
    ts = (ms / 1000.0) if ms else dt.datetime.now(dt.UTC).timestamp()
    return dt.datetime.fromtimestamp(ts, tz=dt.UTC).isoformat().replace("+00:00", "Z")


def _duration_text(ms: float | None) -> str:
    if not ms:
        return ""
    s = int(ms // 1000)
    return f"{s // 60}m {s % 60}s"


class HubSpotAuth:
    kind: Literal["oauth2", "api_key"] = "oauth2"

    def __init__(self, *, client_id: str, client_secret: str, api_base: str, http: httpx.AsyncClient) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.api_base = api_base
        self.http = http
        # VERIFY: minimal scope set for calls/communications/notes.
        self.scopes = [
            "oauth",
            "crm.objects.contacts.read",
            "crm.objects.contacts.write",
            "crm.schemas.calls.write",
        ]

    def authorize_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str:
        q = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(self.scopes),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"https://app.hubspot.com/oauth/authorize?{urlencode(q)}"

    async def _token(self, data: dict[str, str]) -> OAuthTokens:
        resp = await self.http.post(
            f"{self.api_base}/oauth/v1/token",
            data={**data, "client_id": self.client_id, "client_secret": self.client_secret},
        )
        resp.raise_for_status()
        body = resp.json()
        return OAuthTokens(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token"),
            expires_in=body.get("expires_in"),
        )

    async def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> OAuthTokens:
        tokens = await self._token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            }
        )
        info = await self.http.get(f"{self.api_base}/oauth/v1/access-tokens/{tokens.access_token}")
        if info.status_code == 200:
            meta = info.json()
            tokens.extra = {
                "external_user_id": str(meta.get("user_id") or ""),
                "portal_id": str(meta.get("hub_id") or ""),
            }
        return tokens

    async def refresh(self, tokens: OAuthTokens) -> OAuthTokens:
        if not tokens.refresh_token:
            raise ConnectorError("auth", "no refresh token")
        new = await self._token({"grant_type": "refresh_token", "refresh_token": tokens.refresh_token})
        new.extra = tokens.extra
        return new


class HubSpotConnector:
    name = "hubspot"

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        api_base: str = "https://api.hubapi.com",
        portal_id: str = "",
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.portal_id = portal_id
        self.http = http or httpx.AsyncClient(timeout=10.0)
        self.auth: ConnectorAuth = HubSpotAuth(
            client_id=client_id, client_secret=client_secret, api_base=self.api_base, http=self.http
        )
        self.capabilities = {"calls", "sms", "recordings", "notes", "create_contact"}
        self._properties_ready: set[str] = set()

    # --- HTTP -------------------------------------------------------------------------------
    async def _req(self, cred: ConnectorCredential, method: str, path: str, **kw: Any) -> Any:
        resp = await self.http.request(
            method,
            f"{self.api_base}{path}",
            headers={"Authorization": f"Bearer {cred.tokens.access_token}"},
            **kw,
        )
        resp.raise_for_status()
        return resp.json() if resp.content else None

    def _portal(self, cred: ConnectorCredential) -> str:
        return str(cred.tokens.extra.get("portal_id") or self.portal_id or "")

    def _entity(self, cred: ConnectorCredential, obj: dict[str, Any]) -> Entity:
        props = obj.get("properties") or {}
        name = " ".join(p for p in (props.get("firstname"), props.get("lastname")) if p)
        props = {**props, "name": name or props.get("email") or props.get("phone") or ""}
        portal = self._portal(cred)
        url = f"https://app.hubspot.com/contacts/{portal}/record/0-1/{obj['id']}" if portal else None
        return Entity(ref=EntityRef(object_type="contact", id=str(obj["id"])), properties=props, url=url)

    async def _ensure_reference_property(self, cred: ConnectorCredential, object_type: str) -> None:
        portal_key = f"{self._portal(cred)}:{object_type}"
        if portal_key in self._properties_ready:
            return
        resp = await self.http.get(
            f"{self.api_base}/crm/v3/properties/{object_type}/{REFERENCE_PROPERTY}",
            headers={"Authorization": f"Bearer {cred.tokens.access_token}"},
        )
        if resp.status_code == 404:
            await self._req(
                cred,
                "POST",
                f"/crm/v3/properties/{object_type}",
                json={
                    "name": REFERENCE_PROPERTY,
                    "label": "Dialpad activity key",
                    "type": "string",
                    "fieldType": "text",
                    "groupName": "calls" if object_type == "calls" else f"{object_type[:-1]}information",
                    "hasUniqueValue": False,
                },
            )
        elif resp.status_code >= 400:
            resp.raise_for_status()
        self._properties_ready.add(portal_key)

    async def _associate(
        self, cred: ConnectorCredential, from_type: str, from_id: str, to_type: str, to_id: str
    ) -> None:
        await self._req(
            cred, "PUT", f"/crm/v4/objects/{from_type}/{from_id}/associations/default/{to_type}/{to_id}"
        )

    # --- Connector protocol ----------------------------------------------------------------
    def ui_manifest(self) -> UiManifest:
        return UiManifest(
            connector=self.name,
            display_name="HubSpot",
            fields=[
                ManifestField(key="name", label="Name"),
                ManifestField(key="email", label="Email", kind="email"),
                ManifestField(key="phone", label="Phone", kind="phone"),
                ManifestField(key="company", label="Company"),
                ManifestField(key="jobtitle", label="Job title"),
            ],
            link_out_template=None,  # entity.url carries the portal-specific link
            create_fields=[
                ManifestField(key="firstname", label="First name"),
                ManifestField(key="lastname", label="Last name"),
                ManifestField(key="email", label="Email", kind="email"),
                ManifestField(key="phone", label="Phone", kind="phone"),
            ],
        )

    async def search_entity(self, cred: ConnectorCredential, query: EntityQuery) -> list[Entity]:
        groups: list[dict[str, Any]] = []
        for variant in query.phone_variants[:2]:
            for prop in ("phone", "mobilephone"):
                groups.append({"filters": [{"propertyName": prop, "operator": "EQ", "value": variant}]})
        if query.email:
            groups.append({"filters": [{"propertyName": "email", "operator": "EQ", "value": query.email}]})
        body: dict[str, Any] = {"properties": CONTACT_PROPERTIES, "limit": query.limit}
        if groups:
            body["filterGroups"] = groups[:5]
        elif query.text:
            body["query"] = query.text
        else:
            return []
        data = await self._req(cred, "POST", "/crm/v3/objects/contacts/search", json=body)
        return [self._entity(cred, r) for r in (data or {}).get("results", [])]

    async def get_entity(self, cred: ConnectorCredential, ref: EntityRef) -> Entity:
        data = await self._req(
            cred,
            "GET",
            f"/crm/v3/objects/contacts/{ref.id}",
            params={"properties": ",".join(CONTACT_PROPERTIES)},
        )
        return self._entity(cred, data)

    async def create_entity(self, cred: ConnectorCredential, data: NewEntity) -> Entity:
        props = {k: v for k, v in data.properties.items() if v not in (None, "")}
        created = await self._req(cred, "POST", "/crm/v3/objects/contacts", json={"properties": props})
        return self._entity(cred, created)

    def _call_properties(self, act: CallActivity) -> dict[str, Any]:
        inbound = act.direction == "inbound"
        status = CALL_STATUS.get(act.state or "", "COMPLETED")
        title = f"{'Inbound' if inbound else 'Outbound'} Dialpad call"
        if act.state in ("missed", "voicemail"):
            title += f" ({act.state})"
        props: dict[str, Any] = {
            "hs_timestamp": _iso(act.started_at or act.ended_at),
            "hs_call_title": title,
            "hs_call_body": f"{title} {_duration_text(act.duration_ms)}".strip(),
            "hs_call_direction": "INBOUND" if inbound else "OUTBOUND",
            "hs_call_status": status,
            "hs_call_duration": int(act.duration_ms or 0),
            "hs_call_from_number": act.external_number if inbound else act.internal_number,
            "hs_call_to_number": act.internal_number if inbound else act.external_number,
            REFERENCE_PROPERTY: act.activity_key,
        }
        if act.recording_urls:
            props["hs_call_recording_url"] = act.recording_urls[0]
        elif act.voicemail_url:
            props["hs_call_recording_url"] = act.voicemail_url
        return props

    async def upsert_call(self, cred: ConnectorCredential, activity: CallActivity) -> ExternalRef:
        await self._ensure_reference_property(cred, "calls")
        props = self._call_properties(activity)
        if activity.external_ref:
            await self._req(
                cred, "PATCH", f"/crm/v3/objects/calls/{activity.external_ref.id}", json={"properties": props}
            )
            ref = activity.external_ref
        else:
            created = await self._req(cred, "POST", "/crm/v3/objects/calls", json={"properties": props})
            ref = ExternalRef(system=self.name, object_type="calls", id=str(created["id"]))
        if activity.external_contact_ref:
            await self._associate(cred, "calls", ref.id, "contacts", activity.external_contact_ref.id)
        return ref

    async def log_sms(self, cred: ConnectorCredential, activity: SmsLog) -> ExternalRef:
        # VERIFY: HubSpot object used for SMS activity.
        await self._ensure_reference_property(cred, "communications")
        act = activity.activity
        props = {
            "hs_communication_channel_type": "SMS",
            "hs_communication_logged_from": "CRM",
            "hs_communication_body": activity.text or "",
            "hs_timestamp": _iso(act.created_date),
            REFERENCE_PROPERTY: act.activity_key,
        }
        created = await self._req(cred, "POST", "/crm/v3/objects/communications", json={"properties": props})
        ref = ExternalRef(system=self.name, object_type="communications", id=str(created["id"]))
        if act.external_contact_ref:
            await self._associate(cred, "communications", ref.id, "contacts", act.external_contact_ref.id)
        return ref

    async def attach_recording(self, cred: ConnectorCredential, ref: ExternalRef, url: str) -> None:
        # HubSpot holds one recording URL per call; recordings are linked, never copied.
        await self._req(
            cred,
            "PATCH",
            f"/crm/v3/objects/calls/{ref.id}",
            json={"properties": {"hs_call_recording_url": url}},
        )

    async def add_note(self, cred: ConnectorCredential, ref: ExternalRef, note: Note) -> None:
        await self._ensure_reference_property(cred, "notes")
        key = f"dialpad:{note.company_id}:note:{note.note_id}"
        existing = await self.find_by_reference(cred, key)
        if existing is None:
            created = await self._req(
                cred,
                "POST",
                "/crm/v3/objects/notes",
                json={
                    "properties": {
                        "hs_note_body": note.body,
                        "hs_timestamp": _iso(note.created_at * 1000),
                        REFERENCE_PROPERTY: key,
                    }
                },
            )
            existing = ExternalRef(system=self.name, object_type="notes", id=str(created["id"]))
        await self._associate(cred, "notes", existing.id, ref.object_type, ref.id)

    async def find_by_reference(self, cred: ConnectorCredential, activity_key: str) -> ExternalRef | None:
        kind = activity_key.split(":")[2] if activity_key.count(":") >= 3 else "call"
        object_type = OBJECT_FOR_KEY.get(kind, "calls")
        await self._ensure_reference_property(cred, object_type)
        data = await self._req(
            cred,
            "POST",
            f"/crm/v3/objects/{object_type}/search",
            json={
                "filterGroups": [
                    {
                        "filters": [
                            {"propertyName": REFERENCE_PROPERTY, "operator": "EQ", "value": activity_key}
                        ]
                    }
                ],
                "limit": 1,
            },
        )
        results = (data or {}).get("results") or []
        return (
            ExternalRef(system=self.name, object_type=object_type, id=str(results[0]["id"]))
            if results
            else None
        )

    def map_error(self, exc: Exception) -> ConnectorError:
        if isinstance(exc, ConnectorError):
            return exc
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            if status == 401:
                return ConnectorError("auth", "HubSpot token rejected")
            if status == 429:
                ra = exc.response.headers.get("Retry-After")
                return ConnectorError(
                    "rate_limited", "HubSpot rate limit", retry_after=float(ra) if ra else 10.0
                )
            if status == 404:
                return ConnectorError("not_found", exc.response.text[:200])
            if status >= 500:
                return ConnectorError("transient", f"HubSpot {status}")
            return ConnectorError("permanent", f"HubSpot {status}: {exc.response.text[:200]}")
        if isinstance(exc, httpx.TransportError):
            return ConnectorError("transient", str(exc))
        return ConnectorError("transient", repr(exc))
