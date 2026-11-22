"""HubSpotConnector: what the framework calls. Each method delegates to the module that owns it:

==========================  ===============================  =====================
Framework port              Method                           Implemented in
==========================  ===============================  =====================
Connector                   auth                             ``auth.py``
                            ui_manifest                      ``manifest.py``
                            match_contact,                   ``contacts.py``
                            search/get/create_entity
                            map_error                        ``RestConnector`` (SDK)
CallLogger (call_logging)   upsert_call, attach_recording,   ``calls.py``
                            add_note
SmsLogger (sms_logging)     log_sms                          ``messages.py``
both                        find_by_reference                ``references.py``
==========================  ===============================  =====================

Below them: ``mapping.py`` (pure Dialpad → HubSpot translation) and ``crm.py`` (the HubSpot API).
"""

from __future__ import annotations

import httpx

from framework_sanic.sdk import (
    Caller,
    ConnectorCredential,
    ContactMatch,
    Entity,
    EntityMap,
    EntityQuery,
    NewEntity,
    RestConnector,
    UiManifest,
)
from framework_sanic.sdk.call_logging import CallActivity, Note
from framework_sanic.sdk.sms_logging import SmsLog

from .auth import hubspot_auth
from .calls import CallActivities
from .contacts import Contacts
from .crm import CrmClient
from .manifest import MANIFEST
from .messages import SmsActivities
from .references import ActivityReferences


class HubSpotConnector(RestConnector):
    name = "hubspot"
    display_name = "HubSpot"

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        api_base: str = "https://api.hubapi.com",
        portal_id: str = "",
        http: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(base_url=api_base, http=http)
        self.portal_id = portal_id
        self.capabilities = {"calls", "sms", "recordings", "notes", "create_contact"}
        self.auth = hubspot_auth(
            client_id=client_id, client_secret=client_secret, api_base=self.base_url, http=self.http
        )
        crm = CrmClient(self)
        self.references = ActivityReferences(crm, default_portal=portal_id)
        self.contacts = Contacts(crm, default_portal=portal_id)
        self.calls = CallActivities(crm, self.references)
        self.messages = SmsActivities(crm, self.references)

    # --- Connector --------------------------------------------------------------------------
    def ui_manifest(self) -> UiManifest:
        return MANIFEST

    async def match_contact(self, cred: ConnectorCredential, caller: Caller) -> ContactMatch:
        return await self.contacts.match(cred, caller)

    async def search_entity(self, cred: ConnectorCredential, query: EntityQuery) -> list[Entity]:
        return await self.contacts.search(cred, query)

    async def get_entity(self, cred: ConnectorCredential, entity_map: EntityMap) -> Entity:
        return await self.contacts.get(cred, entity_map)

    async def create_entity(self, cred: ConnectorCredential, data: NewEntity) -> Entity:
        return await self.contacts.create(cred, data)

    # --- CallLogger (call_logging) ----------------------------------------------------------
    async def upsert_call(
        self, cred: ConnectorCredential, activity: CallActivity, existing: EntityMap | None
    ) -> EntityMap:
        return await self.calls.upsert(cred, activity, existing)

    async def attach_recording(self, cred: ConnectorCredential, call: EntityMap, url: str) -> None:
        await self.calls.attach_recording(cred, call, url)

    async def add_note(self, cred: ConnectorCredential, call: EntityMap, note: Note) -> EntityMap:
        return await self.calls.add_note(cred, call, note)

    # --- SmsLogger (sms_logging) ------------------------------------------------------------
    async def log_sms(self, cred: ConnectorCredential, activity: SmsLog) -> EntityMap:
        return await self.messages.log(cred, activity)

    # --- CallLogger + SmsLogger -------------------------------------------------------------
    async def find_by_reference(self, cred: ConnectorCredential, activity_key: str) -> EntityMap | None:
        return await self.references.find(cred, activity_key)
