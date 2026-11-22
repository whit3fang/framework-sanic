"""HubSpot CRM API: v3 objects and properties, v4 default associations.

HTTP only, and nothing Dialpad-specific: every method maps to one HubSpot endpoint. Requests go
through the connector (``RestConnector``) for authentication, retries and error mapping.
"""

from __future__ import annotations

from typing import Any

from framework_sanic.sdk import ConnectorCredential, RestConnector

HubSpotObject = dict[str, Any]  # {"id": "...", "properties": {...}}
FilterGroup = dict[str, Any]


def equals(prop: str, value: Any) -> FilterGroup:
    """A search filter group matching ``prop == value``."""
    return {"filters": [{"propertyName": prop, "operator": "EQ", "value": value}]}


class CrmClient:
    def __init__(self, rest: RestConnector) -> None:
        self.rest = rest

    # --- objects (contacts, calls, communications, notes) -----------------------------------
    async def search(
        self,
        cred: ConnectorCredential,
        object_type: str,
        *,
        filter_groups: list[FilterGroup] | None = None,
        query: str | None = None,
        properties: list[str] | None = None,
        limit: int = 10,
    ) -> list[HubSpotObject]:
        """Filter groups are OR-ed (HubSpot allows at most 5); ``query`` is free-text search."""
        body: dict[str, Any] = {"limit": limit}
        if filter_groups:
            body["filterGroups"] = filter_groups[:5]
        if query:
            body["query"] = query
        if properties:
            body["properties"] = properties
        data = await self.rest.post(cred, f"/crm/v3/objects/{object_type}/search", json=body)
        return list((data or {}).get("results", []))

    async def get(
        self, cred: ConnectorCredential, object_type: str, object_id: str, properties: list[str]
    ) -> HubSpotObject:
        data: HubSpotObject = await self.rest.get(
            cred, f"/crm/v3/objects/{object_type}/{object_id}", params={"properties": ",".join(properties)}
        )
        return data

    async def create(
        self, cred: ConnectorCredential, object_type: str, properties: dict[str, Any]
    ) -> HubSpotObject:
        data: HubSpotObject = await self.rest.post(
            cred, f"/crm/v3/objects/{object_type}", json={"properties": properties}
        )
        return data

    async def update(
        self, cred: ConnectorCredential, object_type: str, object_id: str, properties: dict[str, Any]
    ) -> None:
        await self.rest.patch(
            cred, f"/crm/v3/objects/{object_type}/{object_id}", json={"properties": properties}
        )

    # --- associations ------------------------------------------------------------------------
    async def associate(
        self, cred: ConnectorCredential, from_type: str, from_id: str, to_type: str, to_id: str
    ) -> None:
        """Default association (idempotent: PUT)."""
        await self.rest.put(
            cred, f"/crm/v4/objects/{from_type}/{from_id}/associations/default/{to_type}/{to_id}"
        )

    # --- custom properties --------------------------------------------------------------------
    async def property_exists(self, cred: ConnectorCredential, object_type: str, name: str) -> bool:
        resp = await self.rest.send(cred, "GET", f"/crm/v3/properties/{object_type}/{name}")
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        return True

    async def create_property(
        self, cred: ConnectorCredential, object_type: str, definition: dict[str, Any]
    ) -> None:
        resp = await self.rest.send(cred, "POST", f"/crm/v3/properties/{object_type}", json=definition)
        if resp.status_code == 409:  # created meanwhile by another worker: that is what we wanted
            return
        resp.raise_for_status()
