"""STORAGE_ADAPTER=tests.support.custom_storage:<factory> targets for the plug-in tests."""

from __future__ import annotations

from typing import Any

from framework_sanic.adapters.storage.memory.repository import MemoryRepository


class CustomRepository(MemoryRepository):
    """Stands in for a third-party adapter."""


def make(settings: Any, clock: Any) -> CustomRepository:
    return CustomRepository(clock)


def not_a_repository(settings: Any, clock: Any) -> object:
    return object()
