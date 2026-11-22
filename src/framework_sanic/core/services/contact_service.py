"""Contact lookup (live reads with the acting user's credential), rematch and create."""

from __future__ import annotations

import json
import logging
import secrets

from pydantic import BaseModel, Field

from ..domain.entities import ContactLink, ContactLinkHistoryItem, EntityRef
from ..domain.errors import InvalidInput
from ..domain.records import Record, stable_hash
from ..ports.cache import Cache, CacheUnavailable
from ..ports.connector import Connector, ConnectorCredential, Entity, EntityQuery, NewEntity
from ..ports.repository import Repository
from ..ports.runtime import Clock
from .credential_service import ExternalCredentialService
from .phone import phone_variants, to_e164
from .session_service import Principal

log = logging.getLogger(__name__)


class ContactLookup(BaseModel):
    dialpad_ref: str
    entity: Entity | None = None
    linked: bool = False  # True when the match comes from an explicit rematch
    candidates: list[Entity] = Field(default_factory=list)


def number_from_ref(dialpad_ref: str) -> str | None:
    return dialpad_ref.split(":", 1)[1] if dialpad_ref.startswith("phone:") else None


class ContactService:
    def __init__(
        self,
        repo: Repository,
        connector: Connector,
        credentials: ExternalCredentialService,
        cache: Cache,
        clock: Clock,
        *,
        cache_seconds: float = 30.0,
    ) -> None:
        self.repo = repo
        self.connector = connector
        self.credentials = credentials
        self.cache = cache
        self.clock = clock
        self.cache_seconds = cache_seconds

    async def get_link(self, company_id: str, dialpad_ref: str) -> ContactLink | None:
        rec = await self.repo.get(ContactLink.KIND, (company_id, dialpad_ref))
        return ContactLink.from_record(rec) if rec else None

    async def _generation(self, company_id: str, dialpad_ref: str) -> str:
        key = f"contactgen:{company_id}:{stable_hash(dialpad_ref, length=20)}"
        raw = await self.cache.get(key)
        return raw.decode() if raw else "0"

    async def _bump(self, company_id: str, dialpad_ref: str) -> None:
        key = f"contactgen:{company_id}:{stable_hash(dialpad_ref, length=20)}"
        try:
            await self.cache.set(key, secrets.token_hex(4).encode(), 86400)
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

    async def _live_lookup(
        self, company_id: str, cred: ConnectorCredential, dialpad_ref: str, phone: str | None
    ) -> ContactLookup:
        link = await self.get_link(company_id, dialpad_ref)
        if link:
            entity = await self.connector.get_entity(cred, link.entity_ref)
            return ContactLookup(dialpad_ref=dialpad_ref, entity=entity, linked=True)
        number = phone or number_from_ref(dialpad_ref)
        if not number:
            return ContactLookup(dialpad_ref=dialpad_ref)
        candidates = await self.connector.search_entity(
            cred, EntityQuery(phone_variants=phone_variants(number))
        )
        return ContactLookup(
            dialpad_ref=dialpad_ref, entity=candidates[0] if candidates else None, candidates=candidates
        )

    async def search(self, principal: Principal, text: str) -> list[Entity]:
        cred = await self.credentials.resolve(principal.company_id, principal.user_id)
        query = EntityQuery(text=text)
        if any(ch.isdigit() for ch in text) and not any(ch.isalpha() for ch in text):
            query.phone_variants = phone_variants(text)
        elif "@" in text:
            query.email = text
        return await self.connector.search_entity(cred, query)

    async def rematch(self, principal: Principal, dialpad_ref: str, entity_ref: EntityRef) -> ContactLink:
        """Forward-only: affects future activities, never past ones (which keep their stored contact)."""
        now = self.clock.now()

        def merge(cur: Record | None) -> Record | None:
            history: list[ContactLinkHistoryItem] = []
            if cur:
                old = ContactLink.from_record(cur)
                if old.entity_ref == entity_ref:
                    return cur
                history = [
                    *old.history,
                    ContactLinkHistoryItem(
                        entity_ref=old.entity_ref, linked_by=old.linked_by, linked_at=old.linked_at
                    ),
                ][-20:]
            return ContactLink(
                company_id=principal.company_id,
                dialpad_contact_ref=dialpad_ref,
                entity_ref=entity_ref,
                linked_by=principal.user_id,
                linked_at=now,
                history=history,
            ).to_record()

        await self.repo.upsert(ContactLink.KIND, (principal.company_id, dialpad_ref), merge)
        await self._bump(principal.company_id, dialpad_ref)
        link = await self.get_link(principal.company_id, dialpad_ref)
        assert link is not None
        return link

    async def create(self, principal: Principal, dialpad_ref: str, data: NewEntity) -> Entity:
        cred = await self.credentials.resolve(principal.company_id, principal.user_id)
        entity = await self.connector.create_entity(cred, data)
        await self.rematch(principal, dialpad_ref, entity.ref)
        return entity

    async def resolve_for_activity(
        self, company_id: str, cred: ConnectorCredential, dialpad_ref: str | None, number: str | None
    ) -> EntityRef | None:
        """Contact used when an activity is first synced (stage 2). Explicit links win."""
        if dialpad_ref:
            link = await self.get_link(company_id, dialpad_ref)
            if link:
                return link.entity_ref
        number = number or (number_from_ref(dialpad_ref) if dialpad_ref else None)
        if not number:
            return None
        found = await self.connector.search_entity(
            cred, EntityQuery(phone_variants=phone_variants(number), limit=1)
        )
        return found[0].ref if found else None

    async def resolve_or_create(
        self,
        company_id: str,
        cred: ConnectorCredential,
        dialpad_ref: str | None,
        number: str | None,
        *,
        create_if_unknown: bool,
    ) -> EntityRef | None:
        """``resolve_for_activity``, creating a contact for an unknown number if asked to
        (``UNKNOWN_CALLER_POLICY=create_contact``). VERIFY: default behaviour for unknown callers."""
        contact = await self.resolve_for_activity(company_id, cred, dialpad_ref, number)
        if contact is None and number and create_if_unknown:
            created = await self.connector.create_entity(
                cred, NewEntity(properties={"phone": to_e164(number) or number})
            )
            contact = created.ref
        return contact


def serialize_lookup(result: ContactLookup) -> str:
    return json.dumps(result.model_dump(mode="json"))
