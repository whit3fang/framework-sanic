"""Idempotency: every record created for Dialpad carries the Dialpad activity key in a custom
property (``dialpad_activity_key``). Before creating anything the framework asks
``find(activity_key)``, so retried deliveries never create a second record.

The property is created on first use, per portal and object type, and remembered in memory.
"""

from __future__ import annotations

from framework_sanic.sdk import ConnectorCredential, EntityMap

from .crm import CrmClient, equals
from .mapping import REFERENCE_PROPERTY, portal_id

# Activity key "dialpad:{company}:{kind}:{id}" -> the HubSpot object holding that kind.
OBJECT_FOR_KIND = {"call": "calls", "sms": "communications", "note": "notes"}


def object_type_for(activity_key: str) -> str:
    kind = activity_key.split(":")[2] if activity_key.count(":") >= 3 else "call"
    return OBJECT_FOR_KIND.get(kind, "calls")


class ActivityReferences:
    def __init__(self, crm: CrmClient, *, default_portal: str = "") -> None:
        self.crm = crm
        self.default_portal = default_portal
        self._ready: set[str] = set()  # "{portal}:{object_type}" whose property exists

    async def ensure_property(self, cred: ConnectorCredential, object_type: str) -> None:
        marker = f"{portal_id(cred, self.default_portal)}:{object_type}"
        if marker in self._ready:
            return
        if not await self.crm.property_exists(cred, object_type, REFERENCE_PROPERTY):
            await self.crm.create_property(
                cred,
                object_type,
                {
                    "name": REFERENCE_PROPERTY,
                    "label": "Dialpad activity key",
                    "type": "string",
                    "fieldType": "text",
                    "groupName": "calls" if object_type == "calls" else f"{object_type[:-1]}information",
                    "hasUniqueValue": False,
                },
            )
        self._ready.add(marker)

    async def find(self, cred: ConnectorCredential, activity_key: str) -> EntityMap | None:
        object_type = object_type_for(activity_key)
        await self.ensure_property(cred, object_type)
        found = await self.crm.search(
            cred, object_type, filter_groups=[equals(REFERENCE_PROPERTY, activity_key)], limit=1
        )
        return logged(activity_key, object_type, found[0]["id"]) if found else None


def logged(activity_key: str, object_type: str, object_id: object) -> EntityMap:
    """The map the framework stores for a record logged for ``activity_key``."""
    return EntityMap(dialpad_id=activity_key, external_type=object_type, external_id=str(object_id))
