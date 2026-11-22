"""Contact lookup (live reads with the acting user's credential), rematch and create.

Which CRM contact a Dialpad caller is comes from an ``EntityMap`` keyed by the Dialpad contact
reference. When there is none, the connector's ``match_contact`` (the integration's rules)
decides and the result is stored; a rematch replaces it. Automatic matches and rematches are
treated the same. The framework also caches card lookups and applies ``UNKNOWN_CALLER_POLICY``.
"""

from __future__ import annotations

import json
import logging
import secrets
from typing import Literal

from pydantic import BaseModel, Field

from ..domain.entities import EntityMap
from ..domain.errors import InvalidInput
from ..domain.records import stable_hash
from ..ports.cache import Cache, CacheUnavailable
from ..ports.connector import (
    Caller,
    Connector,
    ConnectorCredential,
    Entity,
    EntityQuery,
    NewEntity,
)
from ..ports.runtime import Clock
from .credential_service import ExternalCredentialService
from .entity_map_service import EntityMapService
from .phone import phone_variants, to_e164
from .session_service import Principal

log = logging.getLogger(__name__)


class ContactLookup(BaseModel):
    dialpad_ref: str
    entity: Entity | None = None
    candidates: list[Entity] = Field(default_factory=list)


def number_from_ref(dialpad_ref: str) -> str | None:
    return dialpad_ref.split(":", 1)[1] if dialpad_ref.startswith("phone:") else None


def make_caller(dialpad_ref: str | None, number: str | None, purpose: Literal["card", "activity"]) -> Caller:
    raw = number or (number_from_ref(dialpad_ref) if dialpad_ref else None)
    return Caller(dialpad_ref=dialpad_ref, number=(to_e164(raw) or raw) if raw else None, purpose=purpose)


def contact_map(dialpad_ref: str, entity: Entity) -> EntityMap:
    return EntityMap(
        dialpad_id=dialpad_ref, external_id=entity.external_id, external_type=entity.external_type
    )


class ContactService:
    def __init__(
        self,
        maps: EntityMapService,
        connector: Connector,
        credentials: ExternalCredentialService,
        cache: Cache,
        clock: Clock,
        *,
        cache_seconds: float = 30.0,
    ) -> None:
        self.maps = maps
        self.connector = connector
        self.credentials = credentials
        self.cache = cache
        self.clock = clock
        self.cache_seconds = cache_seconds

    def _gen_key(self, company_id: str, dialpad_ref: str) -> str:
        return f"contactgen:{company_id}:{stable_hash(dialpad_ref, length=20)}"

    async def _generation(self, company_id: str, dialpad_ref: str) -> str:
        raw = await self.cache.get(self._gen_key(company_id, dialpad_ref))
        return raw.decode() if raw else "0"

    async def _bump(self, company_id: str, dialpad_ref: str) -> None:
        try:
            await self.cache.set(self._gen_key(company_id, dialpad_ref), secrets.token_hex(4).encode(), 86400)
        except CacheUnavailable:
            pass

    async def lookup(self, principal: Principal, dialpad_ref: str, phone: str | None = None) -> ContactLookup:
        if not dialpad_ref:
            raise InvalidInput("dialpad_ref is required")
        cache_key: str | None = None
        try:
            gen = await self._generation(principal.company_id, dialpad_ref)
            cache_key = (
                f"contact:{principal.company_id}:{principal.user_id}:{stable_hash(dialpad_ref, phone, gen)}"
            )
            cached = await self.cache.get(cache_key)
            if cached:
                return ContactLookup.model_validate_json(cached)
        except CacheUnavailable:
            cache_key = None
        cred = await self.credentials.resolve(principal.company_id, principal.user_id)
        result = await self._live_lookup(principal.company_id, cred, dialpad_ref, phone)
        if cache_key:
            try:
                await self.cache.set(cache_key, result.model_dump_json().encode(), self.cache_seconds)
            except CacheUnavailable:
                pass
        return result

    async def _mapped_entity(
        self, company_id: str, cred: ConnectorCredential, dialpad_ref: str
    ) -> Entity | None:
        """The stored contact, read live. A map whose CRM record was deleted is dropped (matching
        runs again)."""
        existing = await self.maps.get(company_id, dialpad_ref)
        if existing is None:
            return None
        try:
            return await self.connector.get_entity(cred, existing)
        except Exception as exc:  # noqa: BLE001 - mapped by the connector
            if self.connector.map_error(exc).kind != "not_found":
                raise
            log.info("mapped contact no longer exists in %s; matching again", self.connector.name)
            await self.maps.delete(company_id, dialpad_ref)
            return None

    async def _live_lookup(
        self, company_id: str, cred: ConnectorCredential, dialpad_ref: str, phone: str | None
    ) -> ContactLookup:
        entity = await self._mapped_entity(company_id, cred, dialpad_ref)
        if entity is not None:
            return ContactLookup(dialpad_ref=dialpad_ref, entity=entity)
        match = await self.connector.match_contact(cred, make_caller(dialpad_ref, phone, "card"))
        if match.entity is not None:
            await self.maps.save(company_id, contact_map(dialpad_ref, match.entity), replace=False)
        return ContactLookup(dialpad_ref=dialpad_ref, entity=match.entity, candidates=match.candidates)

    async def search(self, principal: Principal, text: str) -> list[Entity]:
        cred = await self.credentials.resolve(principal.company_id, principal.user_id)
        query = EntityQuery(text=text)
        if any(ch.isdigit() for ch in text) and not any(ch.isalpha() for ch in text):
            query.phone_variants = phone_variants(text)
        elif "@" in text:
            query.email = text
        return await self.connector.search_entity(cred, query)

    async def rematch(
        self, principal: Principal, dialpad_ref: str, external_type: str, external_id: str
    ) -> EntityMap:
        """Replace the caller's contact. Forward-only: activities already logged keep theirs."""
        if not dialpad_ref:
            raise InvalidInput("dialpad_ref is required")
        new = EntityMap(dialpad_id=dialpad_ref, external_type=external_type, external_id=external_id)
        stored = await self.maps.save(principal.company_id, new)
        await self._bump(principal.company_id, dialpad_ref)
        return stored

    async def create(self, principal: Principal, dialpad_ref: str, data: NewEntity) -> Entity:
        cred = await self.credentials.resolve(principal.company_id, principal.user_id)
        entity = await self.connector.create_entity(cred, data)
        await self.rematch(principal, dialpad_ref, entity.external_type, entity.external_id)
        return entity

    async def resolve_for_activity(
        self, company_id: str, cred: ConnectorCredential, dialpad_ref: str | None, number: str | None
    ) -> EntityMap | None:
        """Contact for an activity being logged: the stored map, else ``match_contact`` (stored)."""
        if dialpad_ref:
            existing = await self.maps.get(company_id, dialpad_ref)
            if existing is not None:
                return existing
        match = await self.connector.match_contact(cred, make_caller(dialpad_ref, number, "activity"))
        if match.entity is None:
            return None
        if not dialpad_ref:  # nothing stable to key a map on: use the match for this activity only
            return EntityMap(
                company_id=company_id,
                dialpad_id="",
                external_id=match.entity.external_id,
                external_type=match.entity.external_type,
            )
        return await self.maps.save(company_id, contact_map(dialpad_ref, match.entity), replace=False)

    async def resolve_or_create(
        self,
        company_id: str,
        cred: ConnectorCredential,
        dialpad_ref: str | None,
        number: str | None,
        *,
        create_if_unknown: bool,
    ) -> EntityMap | None:
        """``resolve_for_activity``, creating a contact for an unknown number if asked to
        (``UNKNOWN_CALLER_POLICY=create_contact``). VERIFY: default behaviour for unknown callers."""
        contact = await self.resolve_for_activity(company_id, cred, dialpad_ref, number)
        if contact is None and number and create_if_unknown:
            created = await self.connector.create_entity(
                cred, NewEntity(properties={"phone": to_e164(number) or number})
            )
            new = EntityMap(
                dialpad_id=dialpad_ref or "",
                external_id=created.external_id,
                external_type=created.external_type,
            )
            contact = await self.maps.save(company_id, new) if dialpad_ref else new
        return contact


def serialize_lookup(result: ContactLookup) -> str:
    return json.dumps(result.model_dump(mode="json"))
