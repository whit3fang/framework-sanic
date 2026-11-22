"""No-op ChangeLogSource. VERIFY: the Dialpad change-log API shape and scopes are unknown."""

from __future__ import annotations

from framework_sanic.core.ports.runtime import ChangeEvent


class NoopChangeLogSource:
    async def poll(self, company_id: str, cursor: str | None) -> tuple[list[ChangeEvent], str | None]:
        return [], cursor


class ScriptedChangeLogSource:
    """Test helper: returns queued events once."""

    def __init__(self) -> None:
        self.events: dict[str, list[ChangeEvent]] = {}

    async def poll(self, company_id: str, cursor: str | None) -> tuple[list[ChangeEvent], str | None]:
        return self.events.pop(company_id, []), cursor
