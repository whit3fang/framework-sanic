"""Contacts: matching a Dialpad caller to a HubSpot contact, search (by phone, email or free
text), get and create."""

from __future__ import annotations

from framework_sanic.sdk import (
    Caller,
    ConnectorCredential,
    ContactMatch,
    Entity,
    EntityMap,
    EntityQuery,
    NewEntity,
    phone_variants,
)

from .crm import CrmClient, FilterGroup, equals
from .mapping import CONTACT_PROPERTIES, contact_entity, new_contact_properties, portal_id

PHONE_PROPERTIES = ("phone", "mobilephone")


def contact_filters(query: EntityQuery) -> list[FilterGroup]:
    """OR of: the first two phone variants on each phone property, then the email."""
    groups = [equals(prop, variant) for variant in query.phone_variants[:2] for prop in PHONE_PROPERTIES]
    if query.email:
        groups.append(equals("email", query.email))
    return groups


class Contacts:
    def __init__(self, crm: CrmClient, *, default_portal: str = "") -> None:
        self.crm = crm
        self.default_portal = default_portal

    def _entity(self, cred: ConnectorCredential, obj: dict[str, object]) -> Entity:
        return contact_entity(obj, portal_id(cred, self.default_portal))

    async def match(self, cred: ConnectorCredential, caller: Caller) -> ContactMatch:
        """HubSpot matching rule: a contact whose phone or mobile phone equals one of the caller
        number's common forms; the first result (HubSpot's relevance order) is the match.
        Logging an activity only needs that one; the contact card also offers up to 10
        alternatives for rematch."""
        if not caller.number:
            return ContactMatch()
        limit = 10 if caller.purpose == "card" else 1
        found = await self.search(
            cred, EntityQuery(phone_variants=phone_variants(caller.number), limit=limit)
        )
        return ContactMatch(entity=found[0] if found else None, candidates=found)

    async def search(self, cred: ConnectorCredential, query: EntityQuery) -> list[Entity]:
        filters = contact_filters(query)
        if not filters and not query.text:
            return []
        found = await self.crm.search(
            cred,
            "contacts",
            filter_groups=filters or None,
            query=None if filters else query.text,
            properties=CONTACT_PROPERTIES,
            limit=query.limit,
        )
        return [self._entity(cred, obj) for obj in found]

    async def get(self, cred: ConnectorCredential, contact: EntityMap) -> Entity:
        return self._entity(
            cred, await self.crm.get(cred, "contacts", contact.external_id, CONTACT_PROPERTIES)
        )

    async def create(self, cred: ConnectorCredential, data: NewEntity) -> Entity:
        created = await self.crm.create(cred, "contacts", new_contact_properties(data.properties))
        return self._entity(cred, created)
