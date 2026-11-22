"""The pure layers (no HTTP): Dialpad → HubSpot mapping, contact search filters, key routing."""

from __future__ import annotations

from framework_sanic.sdk import ConnectorCredential, EntityQuery, OAuthTokens
from framework_sanic_hubspot.contacts import contact_filters
from framework_sanic_hubspot.mapping import (
    REFERENCE_PROPERTY,
    call_properties,
    contact_entity,
    new_contact_properties,
    note_properties,
    portal_id,
    sms_properties,
)
from framework_sanic_hubspot.references import object_type_for
from samples import sample_call, sample_note, sample_sms


def test_inbound_call() -> None:
    props = call_properties(sample_call(recording_urls=["https://dialpad.example/rec/1"]))
    assert props["hs_call_title"] == "Inbound Dialpad call"
    assert props["hs_call_body"] == "Inbound Dialpad call 1m 1s"
    assert props["hs_call_direction"] == "INBOUND" and props["hs_call_status"] == "COMPLETED"
    assert (props["hs_call_from_number"], props["hs_call_to_number"]) == ("+14155550100", "+14155550199")
    assert props["hs_call_duration"] == 61_000 and props["hs_timestamp"] == "2023-11-14T22:13:20Z"
    assert props["hs_call_recording_url"] == "https://dialpad.example/rec/1"
    assert props[REFERENCE_PROPERTY] == "dialpad:c1:call:9"


def test_missed_outbound_call_and_voicemail() -> None:
    missed = sample_call().model_copy(
        update={"state": "missed", "direction": "outbound", "duration_ms": None}
    )
    props = call_properties(missed)
    assert (
        props["hs_call_title"] == "Outbound Dialpad call (missed)" and props["hs_call_status"] == "NO_ANSWER"
    )
    assert (props["hs_call_from_number"], props["hs_call_to_number"]) == ("+14155550199", "+14155550100")
    assert "hs_call_recording_url" not in props and props["hs_call_duration"] == 0
    voicemail = sample_call(voicemail_url="https://dialpad.example/vm/1").model_copy(
        update={"state": "voicemail"}
    )
    vm = call_properties(voicemail)
    assert vm["hs_call_recording_url"] == "https://dialpad.example/vm/1"


def test_note_and_sms() -> None:
    note = note_properties(sample_note())
    assert note["hs_note_body"] == "hi" and note[REFERENCE_PROPERTY] == "dialpad:c1:note:note-00001"
    act = sample_sms(created_date=0)
    sms = sms_properties(act, "hello")
    assert sms["hs_communication_channel_type"] == "SMS" and sms["hs_communication_body"] == "hello"
    assert sms[REFERENCE_PROPERTY] == "dialpad:c1:sms:77"
    assert sms_properties(act, None)["hs_communication_body"] == ""


def test_contact_entity_and_portal() -> None:
    cred = ConnectorCredential(
        scope="company", tokens=OAuthTokens(access_token="t", extra={"portal_id": "42"})
    )
    bare = ConnectorCredential(scope="company", tokens=OAuthTokens(access_token="t"))
    assert portal_id(cred, "7") == "42" and portal_id(bare, "7") == "7" and portal_id(bare) == ""
    e = contact_entity({"id": 5, "properties": {"firstname": "Ada", "lastname": "Lovelace"}}, "42")
    assert (
        e.properties["name"] == "Ada Lovelace" and e.url == "https://app.hubspot.com/contacts/42/record/0-1/5"
    )
    assert contact_entity({"id": 6, "properties": {"email": "a@b.c"}}, "").properties["name"] == "a@b.c"
    assert contact_entity({"id": 6, "properties": {}}, "").url is None
    assert new_contact_properties({"firstname": "Bo", "email": "", "phone": None}) == {"firstname": "Bo"}


def test_contact_filters() -> None:
    q = EntityQuery(phone_variants=["+14155550100", "4155550100", "(415) 555-0100"], email="a@b.c")
    groups = contact_filters(q)
    assert [(g["filters"][0]["propertyName"], g["filters"][0]["value"]) for g in groups] == [
        ("phone", "+14155550100"),
        ("mobilephone", "+14155550100"),
        ("phone", "4155550100"),
        ("mobilephone", "4155550100"),
        ("email", "a@b.c"),
    ]
    assert contact_filters(EntityQuery(text="ada")) == []


def test_activity_key_routing() -> None:
    assert object_type_for("dialpad:c1:call:9") == "calls"
    assert object_type_for("dialpad:c1:sms:77") == "communications"
    assert object_type_for("dialpad:c1:note:n1") == "notes"
    assert object_type_for("legacy-key") == "calls"
