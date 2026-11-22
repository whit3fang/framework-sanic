"""Tiny in-memory HubSpot API (httpx MockTransport): objects, search by EQ filters, properties,
default associations. Mimics only the endpoints the connector uses."""

from __future__ import annotations

import json
from typing import Any

import httpx


class FakeHubSpot:
    def __init__(self) -> None:
        self.objects: dict[str, dict[str, dict[str, Any]]] = {
            "contacts": {},
            "calls": {},
            "communications": {},
            "notes": {},
        }
        self.properties: set[str] = set()
        self.associations: set[tuple[str, str, str, str]] = set()
        self.next_id = 100
        self.failing: tuple[int, float | None] | None = None
        self.requests: list[httpx.Request] = []
        self.token = "tok"  # the access token requests must carry

    # --- contract hooks ---------------------------------------------------------------------
    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    def add_contact(self, name: str, phone: str) -> str:
        self.next_id += 1
        self.objects["contacts"][str(self.next_id)] = {"firstname": name, "phone": phone}
        return str(self.next_id)

    def count(self, kind: str) -> int:
        obj = {"call": "calls", "note": "notes", "sms": "communications"}[kind]
        return len(self.objects[obj])

    def fail_with(self, status: int, retry_after: float | None) -> None:
        self.failing = (status, retry_after)

    # --- API --------------------------------------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.failing:
            status, retry_after = self.failing
            headers = {"Retry-After": str(int(retry_after))} if retry_after is not None else {}
            return httpx.Response(status, headers=headers, json={"message": "injected"})
        if request.headers.get("Authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"message": "bad token"})
        parts = request.url.path.strip("/").split("/")
        body = json.loads(request.content) if request.content else {}
        if parts[:3] == ["crm", "v3", "properties"]:
            obj = parts[3]
            if request.method == "GET":
                return httpx.Response(200 if f"{obj}:{parts[4]}" in self.properties else 404, json={})
            self.properties.add(f"{obj}:{body['name']}")
            return httpx.Response(201, json=body)
        if parts[:2] == ["crm", "v4"]:
            self.associations.add((parts[3], parts[4], parts[7], parts[8]))
            return httpx.Response(200, json={})
        obj = parts[3]
        store = self.objects[obj]
        if len(parts) == 5 and parts[4] == "search":
            results = []
            for oid, props in store.items():
                for group in body.get("filterGroups", []):
                    if all(props.get(f["propertyName"]) == f["value"] for f in group["filters"]):
                        results.append({"id": oid, "properties": props})
                        break
            return httpx.Response(200, json={"results": results[: body.get("limit", 10)]})
        if request.method == "POST":
            self.next_id += 1
            oid = str(self.next_id)
            store[oid] = dict(body["properties"])
            return httpx.Response(201, json={"id": oid, "properties": store[oid]})
        oid = parts[4]
        if oid not in store:
            return httpx.Response(404, json={"message": "not found"})
        if request.method == "PATCH":
            store[oid].update(body["properties"])
        return httpx.Response(200, json={"id": oid, "properties": store[oid]})
