"""EntityMap: every contact match (automatic or rematch) is stored and reused; logged activities
are mapped by their activity key."""

from __future__ import annotations

from framework_sanic.core.domain.entities import EntityMap
from framework_sanic.core.ports.connector import ConnectorCredential
from framework_sanic.core.services.session_service import Principal
from framework_sanic.testing import Harness

USER = Principal(company_id="c1", user_id="c1-u1")
CALLER = "phone:+14155550100"


async def test_automatic_match_is_stored_and_reused(harness: Harness) -> None:
    h = harness
    await h.connect_company()
    ada = h.connector.seed_contact("Ada", "+14155550100")
    first = await h.services.contacts.lookup(USER, CALLER)
    assert first.entity is not None and first.entity.external_id == ada.external_id
    stored = await h.services.entity_maps.get("c1", CALLER)
    assert stored == EntityMap(
        company_id="c1",
        dialpad_id=CALLER,
        external_type="contact",
        external_id=ada.external_id,
        updated_at=stored.updated_at if stored else 0,
    )
    # a second contact with the same number appears first in the CRM: the stored match still wins
    imposter = h.connector.seed_contact("Imposter", "+14155550100")
    h.connector.contacts = {imposter.external_id: imposter, **h.connector.contacts}
    h.connector.calls_made.clear()
    again = await h.services.contacts._live_lookup("c1", await _cred(h), CALLER, None)
    assert again.entity is not None and again.entity.external_id == ada.external_id
    assert "search_entity" not in h.connector.calls_made, "a stored map skips matching"


async def test_deleted_contact_is_matched_again(harness: Harness) -> None:
    h = harness
    await h.connect_company()
    ada = h.connector.seed_contact("Ada", "+14155550100")
    await h.services.contacts.rematch(USER, CALLER, "contact", "contact-gone")
    result = await h.services.contacts._live_lookup("c1", await _cred(h), CALLER, None)
    assert result.entity is not None and result.entity.external_id == ada.external_id
    stored = await h.services.entity_maps.get("c1", CALLER)
    assert stored is not None and stored.external_id == ada.external_id


async def test_rematch_replaces_and_first_automatic_match_wins(harness: Harness) -> None:
    h = harness
    maps = h.services.entity_maps
    auto = EntityMap(dialpad_id=CALLER, external_type="contact", external_id="a")
    other = EntityMap(dialpad_id=CALLER, external_type="contact", external_id="b")
    assert (await maps.save("c1", auto, replace=False)).external_id == "a"
    assert (await maps.save("c1", other, replace=False)).external_id == "a"  # concurrent auto match
    assert (await h.services.contacts.rematch(USER, CALLER, "lead", "L7")).external_type == "lead"
    stored = await maps.get("c1", CALLER)
    assert stored is not None and (stored.external_type, stored.external_id) == ("lead", "L7")


async def _cred(h: Harness) -> ConnectorCredential:
    return await h.services.external_credentials.resolve("c1", USER.user_id)
