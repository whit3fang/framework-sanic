"""Local harness feature (FEATURES=fake.dev_harness): seed the fake CRM's contacts and inspect what
the fake listeners (``fake.calls`` / ``fake.sms`` / ``fake.notes``) received.

Refuses to load unless both the platform and the connector are fakes, so it can never be exposed
in a real deployment.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from framework_sanic.core.domain.errors import ConfigurationError
from framework_sanic.core.features.context import FeatureContext
from framework_sanic.http.routes import RequestContext, route
from framework_sanic.stubs.listeners import ReceivedCall, ReceivedNote, ReceivedSms


class Empty(BaseModel):
    pass


class SeedContactIn(BaseModel):
    name: str
    phone: str
    email: str | None = None


class RecordsOut(BaseModel):
    calls: list[dict[str, Any]]
    sms: list[dict[str, Any]]
    notes: list[dict[str, Any]]
    contacts: list[dict[str, Any]]


@route("GET", "/dev/fake-connector/records", auth="none")
async def fake_records(req: Empty, ctx: RequestContext) -> RecordsOut:
    connector: Any = ctx.services.connector
    repo = ctx.services.repo

    async def all_of(kind: str) -> list[dict[str, Any]]:
        return [rec async for rec in repo.query(kind)]

    return RecordsOut(
        calls=await all_of(ReceivedCall.KIND),
        sms=await all_of(ReceivedSms.KIND),
        notes=await all_of(ReceivedNote.KIND),
        contacts=[e.model_dump() for e in connector.contacts.values()],
    )


@route("POST", "/dev/fake-connector/contacts", auth="none", status=201)
async def seed_contact(req: SeedContactIn, ctx: RequestContext) -> RecordsOut:
    connector: Any = ctx.services.connector
    connector.seed_contact(req.name, req.phone, req.email)
    result: RecordsOut = await fake_records(Empty(), ctx)
    return result


class DevHarnessFeature:
    name = "fake.dev_harness"

    def setup(self, ctx: FeatureContext) -> None:
        s = ctx.services.settings
        if s.CONNECTOR != "fake" or s.PLATFORM_ADAPTER != "fake":
            raise ConfigurationError("fake.dev_harness requires CONNECTOR=fake and PLATFORM_ADAPTER=fake")
        ctx.route(fake_records)
        ctx.route(seed_contact)


feature = DevHarnessFeature()
