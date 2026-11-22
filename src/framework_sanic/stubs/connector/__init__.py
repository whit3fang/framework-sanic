"""In-memory stand-in for an external CRM (``CONNECTOR=fake``), for tests and local dev."""

from __future__ import annotations

from typing import Any

from .connector import FakeConnector


def make(settings: Any) -> FakeConnector:
    """Entry point ``framework_sanic.connectors: fake``."""
    return FakeConnector()


__all__ = ["FakeConnector", "make"]
