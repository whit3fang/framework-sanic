"""Reusable connector contract suite: what every connector must do for the framework to work.

Subclass ``ConnectorContract`` and provide the backend. Two fixtures are required and three
unlock stricter checks::

    from framework_sanic.testing.connector_contract import ConnectorContract

    class TestAcmeConnector(ConnectorContract):
        @pytest.fixture
        def connector(self, acme_api) -> Connector:         # required
            return AcmeConnector(base_url="https://api.test", http=acme_api.client())

        @pytest.fixture
        def seed_contact(self, acme_api) -> SeedContact:    # required: (name, phone) -> contact id
            return acme_api.add_contact

        @pytest.fixture
        def record_count(self, acme_api) -> RecordCount:    # optional: no-duplicates checks
            return acme_api.count                          # ("call" | "note" | "sms") -> int

        @pytest.fixture
        def fail_with(self, acme_api) -> FailWith:          # optional: error-mapping checks
            return acme_api.fail_with                      # (status, retry_after) -> None

Feature ports are checked when the connector implements them: ``CallLogger`` (call_logging) and
``SmsLogger`` (sms_logging); the tests for a port it does not implement are skipped.

The backend can be an in-memory fake of the API (fast, for CI), a recorded sandbox
(``framework_sanic.testing.recording``) or a real sandbox account.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any, Literal

import pytest

from framework_sanic.core.domain.entities import EntityMap
from framework_sanic.core.ports.connector import (
    Caller,
    Connector,
    ConnectorCredential,
    ConnectorError,
    EntityQuery,
    NewEntity,
)
from framework_sanic.core.ports.platform import OAuthTokens
from framework_sanic.core.services.phone import phone_variants
from framework_sanic.features.call_logging.models import CallActivity, Note
from framework_sanic.features.call_logging.port import CallLogger
from framework_sanic.features.sms_logging.models import SmsActivity, SmsLog
from framework_sanic.features.sms_logging.port import SmsLogger

SeedContact = Callable[[str, str], str | Awaitable[str]]  # (name, phone) -> external contact id
RecordCount = Callable[[Literal["call", "note", "sms"]], int]
# Make the backend answer every following request with this HTTP status (and Retry-After), so
# the check holds whatever retries the connector does internally.
FailWith = Callable[[int, float | None], None]

PHONE = "+14155550100"


def contract_call(call_id: str = "9", **kw: Any) -> CallActivity:
    """A terminal inbound call as ``call_logging`` hands it to ``upsert_call``."""
    return CallActivity(
        company_id="c1",
        call_id=call_id,
        state="hangup",
        terminal=True,
        direction="inbound",
        external_number=PHONE,
        internal_number="+14155550199",
        duration_ms=61_000,
        started_at=1_700_000_000_000,
        **kw,
    )


def contact_map(contact_id: str, dialpad_id: str = f"phone:{PHONE}") -> EntityMap:
    """A stored contact map, as the framework passes it to ``get_entity`` and on activities."""
    return EntityMap(dialpad_id=dialpad_id, external_type="contact", external_id=contact_id)


def same_record(a: EntityMap | None, b: EntityMap | None) -> bool:
    """Two maps point at the same external record (bookkeeping fields aside)."""
    return (
        a is not None
        and b is not None
        and (a.external_type, a.external_id) == (b.external_type, b.external_id)
    )


def contract_note(note_id: str = "note-00001", call_id: str = "9") -> Note:
    return Note(
        company_id="c1", note_id=note_id, call_id=call_id, author_user_id="u", body="hi", created_at=1.0
    )


class ConnectorContract:
    # Async tests are marked explicitly, so the suite runs without asyncio_mode = "auto".
    # --- fixtures to override ---------------------------------------------------------------
    @pytest.fixture
    def connector(self) -> Connector:
        raise NotImplementedError("override the `connector` fixture")

    @pytest.fixture
    def seed_contact(self) -> SeedContact:
        raise NotImplementedError("override the `seed_contact` fixture")

    @pytest.fixture
    def credential(self) -> ConnectorCredential:
        return ConnectorCredential(
            scope="company", tokens=OAuthTokens(access_token="tok", refresh_token="rt", expires_in=3600)
        )

    @pytest.fixture
    def record_count(self) -> RecordCount | None:
        return None

    @pytest.fixture
    def fail_with(self) -> FailWith | None:
        return None

    @staticmethod
    async def _seed(seed: SeedContact, name: str, phone: str) -> str:
        contact_id = seed(name, phone)
        return str(await contact_id if inspect.isawaitable(contact_id) else contact_id)

    # --- base connector ---------------------------------------------------------------------
    def test_implements_connector(self, connector: Connector) -> None:
        assert isinstance(connector, Connector)
        assert connector.name and connector.name == connector.name.strip()
        assert connector.auth.kind in ("oauth2", "api_key")
        assert isinstance(connector.auth.scopes, list)

    def test_authorize_url_carries_state(self, connector: Connector) -> None:
        if connector.auth.kind != "oauth2":
            pytest.skip("not an OAuth connector")
        url = connector.auth.authorize_url(
            state="contract-state", code_challenge="contract-challenge", redirect_uri="https://app.test/cb"
        )
        assert url.startswith(("http://", "https://")) and "contract-state" in url

    def test_manifest(self, connector: Connector) -> None:
        m = connector.ui_manifest()
        assert m.connector == connector.name and m.display_name and m.fields
        keys = [f.key for f in m.fields]
        assert len(keys) == len(set(keys)), "manifest field keys must be unique"

    @pytest.mark.asyncio
    async def test_search_by_phone_variants(
        self, connector: Connector, seed_contact: SeedContact, credential: ConnectorCredential
    ) -> None:
        contact_id = await self._seed(seed_contact, "Ada", PHONE)
        found = await connector.search_entity(
            credential, EntityQuery(phone_variants=phone_variants("(415) 555-0100"))
        )
        hit = next((e for e in found if e.external_id == contact_id), None)
        assert hit is not None and hit.external_type, "results carry external_id and external_type"
        stored = EntityMap(
            dialpad_id=f"phone:{PHONE}", external_type=hit.external_type, external_id=contact_id
        )
        entity = await connector.get_entity(credential, stored)
        assert (entity.external_type, entity.external_id) == (hit.external_type, contact_id)

    @pytest.mark.asyncio
    async def test_match_contact(
        self, connector: Connector, seed_contact: SeedContact, credential: ConnectorCredential
    ) -> None:
        """The integration's matching finds a contact by the caller's number, for both the
        contact card and activity logging, and reports no match for an unknown caller."""
        contact_id = await self._seed(seed_contact, "Ada", PHONE)
        for purpose in ("card", "activity"):
            caller = Caller(dialpad_ref=f"phone:{PHONE}", number=PHONE, purpose=purpose)
            match = await connector.match_contact(credential, caller)
            assert match.entity is not None and match.entity.external_id == contact_id, purpose
        unknown = await connector.match_contact(
            credential, Caller(dialpad_ref="phone:+14155559876", number="+14155559876")
        )
        assert unknown.entity is None
        assert (await connector.match_contact(credential, Caller())).entity is None

    @pytest.mark.asyncio
    async def test_create_entity(self, connector: Connector, credential: ConnectorCredential) -> None:
        if "create_contact" not in connector.capabilities:
            pytest.skip("connector does not create contacts")
        fields = [f.key for f in connector.ui_manifest().create_fields] or ["name", "phone"]
        values = {"name": "Bo", "firstname": "Bo", "lastname": "Test", "email": "bo@example.com"}
        props = {k: values.get(k, "+15550001" if "phone" in k else "x") for k in fields}
        created = await connector.create_entity(credential, NewEntity(properties=props))
        assert created.external_id and created.external_type

    def test_map_error_passthrough_and_fallback(self, connector: Connector) -> None:
        original = ConnectorError("permanent", "x")
        assert connector.map_error(original) is original
        assert isinstance(connector.map_error(RuntimeError("boom")), ConnectorError)

    @pytest.mark.asyncio
    async def test_http_errors_are_mapped(
        self, connector: Connector, fail_with: FailWith | None, credential: ConnectorCredential
    ) -> None:
        if fail_with is None:
            pytest.skip("provide the `fail_with` fixture to check error mapping")
        cases = (
            (401, "auth"),
            (429, "rate_limited"),
            (503, "transient"),
            (404, "not_found"),
            (400, "permanent"),
        )
        for status, kind in cases:
            fail_with(status, 7.0 if status == 429 else None)
            with pytest.raises(Exception) as exc:  # noqa: B017 - any provider exception type
                await connector.get_entity(credential, contact_map("1"))
            mapped = connector.map_error(exc.value)
            assert mapped.kind == kind, f"HTTP {status} mapped to {mapped.kind}, expected {kind}"
            if kind == "rate_limited":
                assert mapped.retry_after == 7.0, "429 must honour Retry-After"

    # --- CallLogger (call_logging) ----------------------------------------------------------
    @pytest.mark.asyncio
    async def test_call_logging_is_idempotent(
        self,
        connector: Connector,
        seed_contact: SeedContact,
        credential: ConnectorCredential,
        record_count: RecordCount | None,
    ) -> None:
        if not isinstance(connector, CallLogger):
            pytest.skip("connector does not implement CallLogger")
        contact = await self._seed(seed_contact, "Ada", PHONE)
        act = contract_call(contact=contact_map(contact))
        assert await connector.find_by_reference(credential, act.activity_key) is None
        logged = await connector.upsert_call(credential, act, None)
        assert logged.dialpad_id == act.activity_key, "the returned map's dialpad_id is the activity key"
        assert same_record(await connector.find_by_reference(credential, act.activity_key), logged), (
            "find_by_reference must find the record by its activity key (retries rely on it)"
        )
        updated = act.model_copy(update={"duration_ms": 99_000})
        again = await connector.upsert_call(credential, updated, logged)
        assert same_record(again, logged), "an update must keep the record"
        if record_count is not None:
            assert record_count("call") == 1

    @pytest.mark.asyncio
    async def test_call_recordings_and_notes(
        self,
        connector: Connector,
        credential: ConnectorCredential,
        record_count: RecordCount | None,
    ) -> None:
        if not isinstance(connector, CallLogger):
            pytest.skip("connector does not implement CallLogger")
        call = await connector.upsert_call(credential, contract_call("10"), None)
        if "recordings" in connector.capabilities:
            await connector.attach_recording(credential, call, "https://dialpad.example/rec/1")
            await connector.attach_recording(credential, call, "https://dialpad.example/rec/1")
        if "notes" in connector.capabilities:
            note = contract_note(call_id="10")
            first = await connector.add_note(credential, call, note)
            assert first.dialpad_id == note.activity_key
            again = await connector.add_note(credential, call, note)  # duplicate delivery
            assert same_record(again, first), "a redelivered note maps to the same record"
            if record_count is not None:
                assert record_count("note") <= 1, "a redelivered note must not be added twice"

    # --- SmsLogger (sms_logging) ------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_sms_logging_reference(
        self, connector: Connector, credential: ConnectorCredential, record_count: RecordCount | None
    ) -> None:
        if not isinstance(connector, SmsLogger):
            pytest.skip("connector does not implement SmsLogger")
        act = SmsActivity(company_id="c1", sms_id=77, direction="inbound", from_number=PHONE)
        logged = await connector.log_sms(credential, SmsLog(activity=act, text="hello"))
        assert logged.dialpad_id == act.activity_key
        assert same_record(await connector.find_by_reference(credential, act.activity_key), logged)
        if record_count is not None:
            assert record_count("sms") == 1
