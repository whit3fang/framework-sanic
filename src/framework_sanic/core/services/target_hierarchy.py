"""Target hierarchy: company -> office -> {user, department, call center}.

Logging is on by default; configuration is inherited down the hierarchy and can be overridden
per target. ``effective_enabled`` is denormalized on every target and recomputed whenever a
configuration or the hierarchy changes.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..domain.entities import Target
from ..domain.records import Key, Record
from ..ports.platform import TargetInfo
from ..ports.repository import MergeFn, Repository

TKey = tuple[str, str]  # (target_type, target_id)


def compute_effective(targets: Iterable[Target], *, default: bool = True) -> dict[TKey, bool]:
    by_key: dict[TKey, Target] = {(t.target_type, t.target_id): t for t in targets}
    memo: dict[TKey, bool] = {}

    def resolve(k: TKey, seen: frozenset[TKey]) -> bool:
        if k in memo:
            return memo[k]
        t = by_key.get(k)
        if t is None or k in seen:
            return default
        if t.override is not None:
            value = t.override
        elif t.parent_type and t.parent_id:
            value = resolve((t.parent_type, t.parent_id), seen | {k})
        else:
            value = default
        memo[k] = value
        return value

    return {k: resolve(k, frozenset()) for k in by_key}


def _set_field(name: str, value: Any) -> MergeFn:
    def merge(cur: Record | None) -> Record | None:
        return {**cur, name: value} if cur else None

    return merge


class TargetService:
    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    async def all(self, company_id: str, *, include_deleted: bool = False) -> list[Target]:
        out = [Target.from_record(r) async for r in self.repo.query(Target.KIND, company_id=company_id)]
        return [t for t in out if include_deleted or not t.deleted]

    async def get(self, company_id: str, target_type: str, target_id: str) -> Target | None:
        rec = await self.repo.get(Target.KIND, (company_id, target_type, target_id))
        return Target.from_record(rec) if rec else None

    async def is_logging_enabled(
        self, company_id: str, target_type: str | None, target_id: str | None
    ) -> bool:
        if not target_type or not target_id:
            return True
        t = await self.get(company_id, target_type, target_id)
        if t is None or t.deleted:
            return True  # on by default; unknown targets are picked up by the next reconcile
        return t.effective_enabled

    async def sync_from_platform(self, company_id: str, infos: list[TargetInfo]) -> bool:
        """Upsert the hierarchy from platform list calls (preserving overrides). Returns True if changed."""
        seen: set[TKey] = set()
        changed = False
        for info in infos:
            seen.add((info.target_type, info.target_id))

            def merge(cur: Record | None, info: TargetInfo = info) -> Record | None:
                base = (
                    Target.from_record(cur)
                    if cur
                    else Target(company_id=company_id, target_type=info.target_type, target_id=info.target_id)
                )
                return base.model_copy(
                    update={
                        "name": info.name,
                        "parent_type": info.parent_type,
                        "parent_id": info.parent_id,
                        "deleted": False,
                    }
                ).to_record()

            res = await self.repo.upsert(Target.KIND, (company_id, info.target_type, info.target_id), merge)
            changed |= res in ("created", "updated")
        for t in await self.all(company_id):
            if (t.target_type, t.target_id) not in seen:
                await self.repo.upsert(Target.KIND, t.key(), _set_field("deleted", True))
                changed = True
        await self.recompute(company_id)
        return changed

    async def set_override(
        self, company_id: str, target_type: str, target_id: str, override: bool | None
    ) -> Target:
        key: Key = (company_id, target_type, target_id)

        def merge(cur: Record | None) -> Record | None:
            base = (
                Target.from_record(cur)
                if cur
                else Target(company_id=company_id, target_type=target_type, target_id=target_id)
            )
            return base.model_copy(update={"override": override}).to_record()

        await self.repo.upsert(Target.KIND, key, merge)
        await self.recompute(company_id)
        result = await self.get(company_id, target_type, target_id)
        assert result is not None
        return result

    async def recompute(self, company_id: str) -> int:
        """Recompute ``effective_enabled`` for the whole company; returns the number of changed targets."""
        targets = await self.all(company_id, include_deleted=True)
        effective = compute_effective(t for t in targets if not t.deleted)
        changed = 0
        for t in targets:
            value = effective.get((t.target_type, t.target_id), True)
            if t.effective_enabled != value:
                res = await self.repo.upsert(Target.KIND, t.key(), _set_field("effective_enabled", value))
                changed += res == "updated"
        return changed
