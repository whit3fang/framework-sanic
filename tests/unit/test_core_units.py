from __future__ import annotations

import asyncio

import pytest

from framework_sanic.adapters.cache.memory import MemoryCache
from framework_sanic.adapters.kms.local import LocalKeyProvider
from framework_sanic.bootstrap import build_services
from framework_sanic.core.domain.errors import ConfigurationError, Unauthorized
from framework_sanic.core.domain.events import CallEvent, SmsEvent, classify_event
from framework_sanic.core.domain.records import Lane, shard_for
from framework_sanic.core.features.context import FeatureContext, FeatureRegistry, validate_scopes
from framework_sanic.core.services.crypto import EnvelopeCipher
from framework_sanic.core.services.phone import phone_variants, to_e164
from framework_sanic.core.services.rate_limit import RateLimiter
from framework_sanic.core.services.session_service import Principal, SessionService
from tests.conftest import FakeClock, make_settings


class CountingProvider(LocalKeyProvider):
    def __init__(self) -> None:
        super().__init__("a2V5")
        self.wraps = 0
        self.unwraps = 0

    async def wrap(self, dek: bytes) -> bytes:
        self.wraps += 1
        return await super().wrap(dek)

    async def unwrap(self, wrapped: bytes) -> bytes:
        self.unwraps += 1
        return await super().unwrap(wrapped)


async def test_envelope_roundtrip_and_dek_reuse() -> None:
    p = CountingProvider()
    c = EnvelopeCipher(p)
    blobs = [await c.encrypt_str(f"msg {i}", "aad") for i in range(20)]
    assert p.wraps == 1  # DEK reused: no KMS call per message
    assert [await c.decrypt_str(b, "aad") for b in blobs] == [f"msg {i}" for i in range(20)]
    other = EnvelopeCipher(p)  # another process: unwraps once, then caches
    assert await other.decrypt_str(blobs[0], "aad") == "msg 0"
    await other.decrypt_str(blobs[1], "aad")
    assert p.unwraps == 1
    from cryptography.exceptions import InvalidTag

    with pytest.raises(InvalidTag):
        await c.decrypt_str(blobs[0], "wrong-aad")


def test_phone_helpers() -> None:
    assert to_e164("(415) 555-0100") == "+14155550100"
    assert to_e164("0044 20 7946 0000") == "+442079460000"
    assert to_e164("020 7946 0000", "44") == "+442079460000"
    v = phone_variants("+14155550100")
    assert v[:4] == ["+14155550100", "14155550100", "4155550100", "04155550100"]


def test_shard_for_is_stable_and_bounded() -> None:
    assert shard_for("c1", 8) == shard_for("c1", 8)
    assert {shard_for(f"c{i}", 8) for i in range(200)} == set(range(8))
    assert shard_for("anything", 1) == 0


def test_event_classification() -> None:
    assert classify_event({"call_id": 1, "state": "hangup"}) == "call"
    assert classify_event({"id": 1, "text": "x"}) == "sms"
    assert classify_event({"foo": 1}) is None
    ev = CallEvent.model_validate(
        {
            "call_id": 1,
            "state": "HANGUP",
            "event_timestamp": "12",
            "target": {"id": 9, "type": "department"},
            "operator": {"target": {"id": 77}},
        }
    )
    assert ev.state == "hangup" and ev.event_timestamp == 12 and ev.is_department_leg
    assert ev.operator_user_id() == "77"
    assert SmsEvent.model_validate({"id": "123", "to_number": "+1"}).to_number == ["+1"]


async def test_session_tokens() -> None:
    cache = MemoryCache()
    clock = FakeClock()
    s = SessionService("x" * 32, 60, cache, clock)
    token, _ = s.issue(Principal(company_id="c1", user_id="u1", is_admin=True))
    p = await s.verify(token)
    assert (p.company_id, p.user_id, p.is_admin) == ("c1", "u1", True)
    cache.down = True
    assert (await s.verify(token)).user_id == "u1"  # revocation check fails soft
    cache.down = False
    await s.revoke(p.session_id)  # type: ignore[arg-type]
    with pytest.raises(Unauthorized):
        await s.verify(token)
    token2, _ = s.issue(Principal(company_id="c1", user_id="u1"))
    clock.advance(61)
    with pytest.raises(Unauthorized):
        await s.verify(token2)


async def test_rate_limiter_lanes_and_fallback() -> None:
    cache = MemoryCache()
    rl = RateLimiter(cache)
    granted = 0
    while await rl.acquire("b", per_minute=10, lane=Lane.BACKFILL) == 0:
        granted += 1
    assert granted == 7  # backfill leaves a 30% reserve
    assert await rl.acquire("b", per_minute=10, lane=Lane.UI) == 0  # UI can still go
    cache.down = True
    assert await rl.acquire("b2", per_minute=10, lane=Lane.UI) == 0  # local fallback bucket


def test_scope_validation_refuses_start() -> None:
    reg = FeatureRegistry()
    ctx = FeatureContext(name="x", services=None)  # type: ignore[arg-type]
    ctx.require_scopes("calls:list", "secret_scope")
    reg.contexts.append(ctx)
    with pytest.raises(ConfigurationError, match="secret_scope"):
        validate_scopes(reg, ["calls:list"])
    with pytest.raises(ConfigurationError):
        build_services(make_settings(DIALPAD_SCOPES="calls:list"))


def test_features_load_and_integration_feature() -> None:
    s = build_services(make_settings(FEATURES="call_logging,sms_logging,hubspot.sms_campaign"))
    assert s.features is not None
    names = [c.name for c in s.features.contexts]
    assert names == ["platform", "call_logging", "sms_logging", "hubspot.sms_campaign"]
    assert ("events", "event.call:call_logging") in s.features.jobs
    assert ("events", "event.sms:sms_logging") in s.features.jobs
    assert s.reconciler.config.subscription_kinds == ("call", "sms")
    # The logging features are opt-in: without them the platform subscribes to and processes nothing.
    bare = build_services(make_settings(FEATURES="hubspot.sms_campaign"))
    assert bare.features is not None and bare.features.event_routes == {}
    assert bare.reconciler.config.subscription_kinds == ()


def test_feature_name_resolution() -> None:
    from framework_sanic.core.features.context import resolve_feature_module as r

    assert r("platform") == "framework_sanic.core.features.platform"
    assert r("call_logging") == "framework_sanic.features.call_logging"
    assert r("sms_logging") == "framework_sanic.features.sms_logging"
    assert r("hubspot.sms_campaign") == "framework_sanic.connectors.hubspot.features.sms_campaign"
    assert r("fake.dev_harness") == "framework_sanic.stubs.connector.features.dev_harness"
    assert r("tests.support.echo_feature") == "tests.support.echo_feature"


async def test_scheduler_runs_jobs() -> None:
    from framework_sanic.adapters.scheduler.memory import AsyncioScheduler

    sched = AsyncioScheduler()
    hits: list[int] = []

    async def job() -> None:
        hits.append(1)

    sched.every("j", 0.01, job)
    await sched.start()
    await asyncio.sleep(0.05)
    await sched.stop()
    assert len(hits) >= 2
