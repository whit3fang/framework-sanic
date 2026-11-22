"""Contact card endpoints: live reads with the acting user's credential, rematch, create."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from framework_sanic.core.domain.entities import EntityRef
from framework_sanic.core.domain.errors import InvalidInput
from framework_sanic.core.ports.connector import Entity, NewEntity, UiManifest

from ..routes import RequestContext, route


class ContactQuery(BaseModel):
    dialpad_ref: str = Field(min_length=3, max_length=200)
    phone: str | None = Field(default=None, max_length=40)


class ContactOut(BaseModel):
    dialpad_ref: str
    entity: Entity | None
    linked: bool
    candidates: list[Entity]
    manifest: UiManifest


class SearchQuery(BaseModel):
    q: str = Field(min_length=2, max_length=200)


class SearchOut(BaseModel):
    results: list[Entity]


class RematchIn(BaseModel):
    dialpad_ref: str = Field(min_length=3, max_length=200)
    entity_ref: EntityRef
    confirm_forward_only: bool = False


class RematchOut(BaseModel):
    dialpad_ref: str
    entity_ref: EntityRef
    applies_to: str = "future_activities_only"


class CreateContactIn(BaseModel):
    dialpad_ref: str = Field(min_length=3, max_length=200)
    properties: dict[str, Any]

    @field_validator("properties")
    @classmethod
    def _small_strings(cls, v: dict[str, Any]) -> dict[str, Any]:
        if len(v) > 30:
            raise ValueError("too many properties")
        for key, value in v.items():
            if not isinstance(value, (str, int, float, bool)) or len(str(value)) > 500 or len(key) > 100:
                raise ValueError(f"invalid property {key!r}")
        return v


@route("GET", "/contact", auth="session")
async def get_contact(req: ContactQuery, ctx: RequestContext) -> ContactOut:
    s = ctx.services
    result = await s.contacts.lookup(ctx.require_principal(), req.dialpad_ref, req.phone)
    return ContactOut(**result.model_dump(), manifest=s.connector.ui_manifest())


@route("GET", "/contact/search", auth="session")
async def search_contacts(req: SearchQuery, ctx: RequestContext) -> SearchOut:
    return SearchOut(results=await ctx.services.contacts.search(ctx.require_principal(), req.q))


@route("POST", "/contact/rematch", auth="session")
async def rematch_contact(req: RematchIn, ctx: RequestContext) -> RematchOut:
    """Forward-only: affects future activities, never past ones."""
    if not req.confirm_forward_only:
        raise InvalidInput("rematch applies to future activities only; set confirm_forward_only=true")
    link = await ctx.services.contacts.rematch(ctx.require_principal(), req.dialpad_ref, req.entity_ref)
    return RematchOut(dialpad_ref=link.dialpad_contact_ref, entity_ref=link.entity_ref)


@route("POST", "/contact", auth="session", status=201)
async def create_contact(req: CreateContactIn, ctx: RequestContext) -> Entity:
    if "create_contact" not in ctx.services.connector.capabilities:
        raise InvalidInput("this connector cannot create contacts")
    return await ctx.services.contacts.create(
        ctx.require_principal(), req.dialpad_ref, NewEntity(properties=req.properties)
    )
