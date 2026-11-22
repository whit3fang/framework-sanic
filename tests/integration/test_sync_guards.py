"""SyncGuards: what a listener's job checks before calling the external system (parking,
credentials, rate limits, leases, error mapping). Also the outbox relay."""

from __future__ import annotations

from framework_sanic.core.domain.entities import OutboxEntry
from framework_sanic.core.domain.records import Lane
from framework_sanic.core.features.guards import SyncGuards
from framework_sanic.core.ports.connector import ConnectorCredential, ConnectorError
from framework_sanic.core.ports.platform import OAuthTokens
from framework_sanic.core.ports.queue import TOPIC_SYNC, QueueMessage
from framework_sanic.core.services.outbox import OutboxRelay
from framework_sanic.testing import Harness, start_harness

MSG = QueueMessage(
    message_id="m", topic=TOPIC_SYNC, company_id="c1", lane=Lane.LIVE, job="x.sync", payload={}
)


async def test_check_returns_a_credential_when_clear() -> None:
    h = await start_harness()
    cred = await SyncGuards(h.services).check(MSG, "c1-u1")
    assert isinstance(cred, ConnectorCredential) and cred.scope == "company"


async def test_parked_company_and_missing_credential_defer_without_counting() -> None:
    h = await start_harness()
    await h.services.companies.park("c1", "credential revoked")
    parked = await SyncGuards(h.services).check(MSG, None)
    assert (
        not isinstance(parked, ConnectorCredential) and parked.action == "retry" and not parked.count_attempt
    )
    no_cred = await start_harness(connect=False)
    await no_cred.connect_company(external=False)
    result = await SyncGuards(no_cred.services).check(MSG, None)
    assert (
        not isinstance(result, ConnectorCredential) and result.action == "retry" and not result.count_attempt
    )


async def test_rate_limit_defers_without_counting() -> None:
    h = await start_harness(CONNECTOR_RATE_LIMIT_PER_MINUTE=60)
    for _ in range(60):
        await h.services.limiter.acquire("connector:fake:c1", per_minute=60)
    result = await SyncGuards(h.services).check(MSG, None)
    assert (
        not isinstance(result, ConnectorCredential) and result.action == "retry" and not result.count_attempt
    )


async def test_lease_is_exclusive_until_released() -> None:
    h = await start_harness()
    guards = SyncGuards(h.services)
    mine = await guards.lease("k")
    assert mine and await guards.lease("k") is None
    await guards.release("k", "someone-else")  # not ours: must not free it
    assert await guards.lease("k") is None
    await guards.release("k", mine)
    assert await guards.lease("k")


async def test_errors_are_mapped_to_job_results(harness: Harness) -> None:
    h = harness
    await h.connect_company()
    await h.services.external_credentials.save("c1", "user", "c1-u1", OAuthTokens(access_token="user-token"))
    guards = SyncGuards(h.services)
    user = ConnectorCredential(scope="user", user_id="c1-u1", tokens=OAuthTokens(access_token="user-token"))
    auth = await guards.on_error(ConnectorError("auth"), MSG, user)
    assert auth.action == "retry" and not auth.count_attempt
    stored = await h.services.token_store.get_external("c1", "user", "c1-u1")
    assert stored is not None and stored.status == "bad"  # the next attempt uses another credential
    limited = await guards.on_error(ConnectorError("rate_limited", retry_after=7.0), MSG, user)
    assert limited.action == "retry" and limited.delay >= 7.0 and not limited.count_attempt
    assert (await guards.on_error(ConnectorError("transient"), MSG, user)).count_attempt
    assert (await guards.on_error(ConnectorError("permanent"), MSG, user)).action == "deadletter"


async def test_outbox_relay_republishes(harness: Harness) -> None:
    h = harness
    entry = OutboxEntry(
        company_id="c1",
        outbox_id="x.sync:c1:9",
        topic=TOPIC_SYNC,
        lane="live",
        job="x.sync",
        payload={"id": "9"},
        created_at=h.clock.now() - 60,
    )
    await h.repo.upsert(OutboxEntry.KIND, entry.key(), lambda cur: entry.to_record())
    assert await OutboxRelay(h.repo, h.queue, h.clock).relay_once() == 1
    assert [m.message_id for m in h.queue.published] == ["x.sync:c1:9"]
    assert [r async for r in h.repo.query(OutboxEntry.KIND)] == []
