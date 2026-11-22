"""Storage is pluggable: built-in names, or ``package.module:factory`` for your own adapter."""

from __future__ import annotations

import pytest

from framework_sanic.bootstrap import build_services, make_repository, startup
from framework_sanic.core.domain.errors import ConfigurationError
from framework_sanic.core.ports.repository import IndexSpec
from framework_sanic.testing import FakeClock, make_settings
from tests.support.custom_storage import CustomRepository


def test_factory_path_loads_a_custom_adapter() -> None:
    repo = make_repository(make_settings(STORAGE_ADAPTER="tests.support.custom_storage:make"), FakeClock())
    assert isinstance(repo, CustomRepository)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("postgres", "unknown STORAGE_ADAPTER"),
        ("tests.support.custom_storage:missing", "cannot be loaded"),
        ("no.such.module:make", "cannot be loaded"),
        ("tests.support.custom_storage:not_a_repository", "does not implement the Repository port"),
    ],
)
def test_bad_storage_adapter_fails_at_startup(value: str, message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        make_repository(make_settings(STORAGE_ADAPTER=value), FakeClock())


async def test_setup_receives_kinds_and_declared_indexes() -> None:
    s = build_services(
        make_settings(STORAGE_ADAPTER="tests.support.custom_storage:make", FEATURES="fake.calls,fake.sms")
    )
    await startup(s, schedules=False)
    schema = s.repo.schema  # type: ignore[attr-defined]
    assert {"Company", "Outbox", "AuditEvent", "EntityMap", "FakeReceivedCall", "FakeReceivedSms"} <= set(
        schema.kinds
    )
    assert len(schema.kinds) == len(set(schema.kinds))
    assert IndexSpec("Target", ("company_id", "effective_enabled")) in schema.indexes


async def test_purge_is_scheduled_and_deletes_expired_records() -> None:
    clock = FakeClock()
    s = build_services(make_settings(PURGE_BATCH_SIZE=2), clock=clock)
    spec = next(x for x in s.features.schedules if x.name == "platform.purge_expired")  # type: ignore[union-attr]
    for i in range(5):
        await s.repo.upsert(
            "X", ("c1", str(i)), lambda _: {"schema_version": 1, "expires_at": clock.now() + 1}
        )
    clock.advance(2)
    await spec.fn()  # loops batches until a short one
    assert s.repo._data["X"] == {}  # type: ignore[attr-defined]
