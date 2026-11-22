"""HubSpotConnector: the framework's ``Connector`` port plus the HubSpot operations this
integration's listeners use. Each method delegates to the module that owns it:

======================================  ===============================  =====================
Used by                                 Method                           Implemented in
======================================  ===============================  =====================
the framework (``Connector``)           auth                             ``auth.py``
                                        ui_manifest                      ``manifest.py``
                                        match_contact,                   ``contacts.py``
                                        search/get/create_entity
                                        map_error                        ``RestConnector`` (SDK)
``listeners/calls.py``, ``notes.py``    upsert_call, attach_recording,   ``calls.py``
                                        add_note
``listeners/sms.py``                    log_sms                          ``messages.py``
all listeners                           find_by_reference                ``references.py``
======================================  ===============================  =====================

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

from .activities import CallNote, LoggedCall, LoggedSms
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

    # --- call logging (listeners/calls.py, listeners/notes.py) ------------------------------
    async def upsert_call(
        self, cred: ConnectorCredential, activity: LoggedCall, existing: EntityMap | None
    ) -> EntityMap:
        return await self.calls.upsert(cred, activity, existing)

    async def attach_recording(self, cred: ConnectorCredential, call: EntityMap, url: str) -> None:
        await self.calls.attach_recording(cred, call, url)

    async def add_note(self, cred: ConnectorCredential, call: EntityMap, note: CallNote) -> EntityMap:
        return await self.calls.add_note(cred, call, note)

    # --- SMS logging (listeners/sms.py) -----------------------------------------------------
    async def log_sms(self, cred: ConnectorCredential, sms: LoggedSms, text: str | None) -> EntityMap:
        """``text`` is the transiently decrypted message: sent to HubSpot, never stored."""
        return await self.messages.log(cred, sms, text)

    # --- both -------------------------------------------------------------------------------
    async def find_by_reference(self, cred: ConnectorCredential, activity_key: str) -> EntityMap | None:
        return await self.references.find(cred, activity_key)
