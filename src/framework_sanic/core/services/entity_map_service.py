"""Storage of ``EntityMap`` rows: Dialpad id -> external record, one per company and Dialpad id."""

from __future__ import annotations

from ..domain.entities import EntityMap
from ..domain.records import Record
from ..ports.repository import Repository
from ..ports.runtime import Clock


class EntityMapService:
    def __init__(self, repo: Repository, clock: Clock) -> None:
        self.repo = repo
        self.clock = clock

    async def get(self, company_id: str, dialpad_id: str) -> EntityMap | None:
        rec = await self.repo.get(EntityMap.KIND, (company_id, dialpad_id))
        return EntityMap.from_record(rec) if rec else None

    async def save(
        self, company_id: str, entity_map: EntityMap, *, dialpad_id: str | None = None, replace: bool = True
    ) -> EntityMap:
        """Store ``entity_map`` for ``company_id`` (under ``dialpad_id`` when given). ``replace=False``
        keeps an existing row (first writer wins, e.g. two concurrent automatic matches); returns
        the row now stored."""
        update = {"company_id": company_id, "updated_at": self.clock.now()}
        new = entity_map.model_copy(update={**update, **({"dialpad_id": dialpad_id} if dialpad_id else {})})

        def merge(cur: Record | None) -> Record | None:
            if cur is not None:
                old = EntityMap.from_record(cur)
                same = (old.external_type, old.external_id) == (new.external_type, new.external_id)
                if same or not replace:
                    return cur
            return new.to_record()

        await self.repo.upsert(EntityMap.KIND, new.key(), merge)
        stored = await self.get(company_id, new.dialpad_id)
        assert stored is not None
        return stored

    async def delete(self, company_id: str, dialpad_id: str) -> None:
        await self.repo.delete(EntityMap.KIND, (company_id, dialpad_id))
