"""Storage-level primitives shared by every port and adapter."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any, Literal

Key = tuple[str | int, ...]
Record = dict[str, Any]
UpsertResult = Literal["created", "updated", "unchanged", "ignored"]


class Lane(StrEnum):
    """Priority lanes, highest first: UI-initiated > live call terminal events > backfill/sweep."""

    UI = "ui"
    LIVE = "live"
    BACKFILL = "backfill"


LANE_ORDER: tuple[Lane, ...] = (Lane.UI, Lane.LIVE, Lane.BACKFILL)


def stable_hash(*parts: Any, length: int = 32) -> str:
    """Deterministic hash used for deterministic keys, dedupe keys and shard selection."""
    raw = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:length]


def shard_for(company_id: str, shard_count: int) -> int:
    """Queues are sharded by company hash."""
    if shard_count <= 1:
        return 0
    return int(hashlib.sha1(company_id.encode()).hexdigest()[:8], 16) % shard_count


def key_to_str(key: Key) -> str:
    """Canonical string form of a key (used by adapters that need a single string id)."""
    return json.dumps(list(key), separators=(",", ":"))


def key_from_str(raw: str) -> Key:
    return tuple(json.loads(raw))
