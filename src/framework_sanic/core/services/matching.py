"""Optional helper for ``Connector.match_contact``. Matching rules belong to the integration; this
is a starting point for CRMs where "first contact whose phone matches" is good enough."""

from __future__ import annotations

from ..ports.connector import Caller, Connector, ConnectorCredential, ContactMatch, EntityQuery
from .phone import phone_variants


async def match_by_phone(
    connector: Connector, cred: ConnectorCredential, caller: Caller, *, limit: int = 10
) -> ContactMatch:
    """``search_entity`` by the caller number's common forms; the first result is the match."""
    if not caller.number:
        return ContactMatch()
    found = await connector.search_entity(
        cred, EntityQuery(phone_variants=phone_variants(caller.number), limit=limit)
    )
    return ContactMatch(entity=found[0] if found else None, candidates=found)
