"""Gate 7: target hierarchy, reconciler (no duplicates across repeated and concurrent runs),
weekly staggered sweep, change-log placeholder."""

from __future__ import annotations

import asyncio
from collections import Counter

from framework_sanic.adapters.changelog.noop import ScriptedChangeLogSource
from framework_sanic.core.domain.entities import Target
from framework_sanic.core.ports.platform import RemoteSubscription, TargetInfo
from framework_sanic.core.ports.runtime import ChangeEvent
from framework_sanic.core.services.subscription_reconciler import (
    WEEK_SECONDS,
    ChangeLogWatcher,
    Sweeper,
    due_in_window,
    sweep_slot,
)
from framework_sanic.core.services.target_hierarchy import compute_effective
from tests.conftest import Harness


def remote_keys(h: Harness, company: str = "c1") -> Counter[tuple[str, str | None, str | None]]:
    return Counter((s.kind, s.target_type, s.target_id) for s in h.platform.subs.get(company, {}).values())


async def test_reconcile_creates_per_target_subscriptions(harness: Harness) -> None:
    h = harness
    await h.connect_company()
    report = await h.services.reconciler.reconcile("c1")
    keys = remote_keys(h)
    loggable = [
        t for t in h.platform.companies["c1"].targets if t.target_type in ("user", "department", "callcenter")
    ]
    assert len(keys) == 2 * len(loggable) and set(keys.values()) == {1}
    assert report.created == 2 * len(loggable)
    sub = next(iter(h.platform.subs["c1"].values()))
    endpoint = next(iter(h.platform.webhooks["c1"]))
    assert sub.endpoint_id == endpoint
    url, secret = h.platform.webhooks["c1"][endpoint]
    assert url == "http://testserver/webhooks/dialpad/c1/events" and secret == h.services.webhooks.secret_for(
        "c1"
    )
    call_sub = next(s for s in h.platform.subs["c1"].values() if s.kind == "call")
    assert call_sub.settings["call_states"] == ["hangup", "missed", "voicemail", "recording"]
    sms_sub = next(s for s in h.platform.subs["c1"].values() if s.kind == "sms")
    assert sms_sub.settings == {"direction": "all", "include_internal": False, "status": False}


async def test_repeated_runs_are_idempotent(harness: Harness) -> None:
    h = harness
    await h.connect_company()
    await h.services.reconciler.reconcile("c1")
    before = dict(h.platform.subs["c1"])
    for _ in range(3):
        report = await h.services.reconciler.reconcile("c1")
        assert report.created == report.updated == report.deleted == 0
    assert h.platform.subs["c1"].keys() == before.keys()


async def test_concurrent_runs_never_duplicate(harness: Harness) -> None:
    h = harness
    await h.connect_company()
    h.platform.latency = 0.001  # force interleaving
    h.cache.down = True  # no Redis lease: correctness must come from the repository claim
    await asyncio.gather(*(h.services.reconciler.reconcile("c1", use_lease=False) for _ in range(6)))
    assert set(remote_keys(h).values()) == {1}
    h.cache.down = False
    await asyncio.gather(*(h.services.reconciler.reconcile("c1") for _ in range(6)))
    assert set(remote_keys(h).values()) == {1}


async def test_existing_duplicates_and_orphans_are_cleaned(harness: Harness) -> None:
    h = harness
    await h.connect_company()
    await h.services.reconciler.reconcile("c1")
    endpoint = next(iter(h.platform.webhooks["c1"]))
    dup = RemoteSubscription(
        id="sub-dup",
        kind="call",
        target_type="user",
        target_id="c1-u1",
        endpoint_id=endpoint,
        settings={"call_states": ["hangup"]},
    )
    orphan = RemoteSubscription(
        id="sub-orphan", kind="call", target_type="user", target_id="gone", endpoint_id=endpoint
    )
    h.platform.subs["c1"].update({dup.id: dup, orphan.id: orphan})
    report = await h.services.reconciler.reconcile("c1")
    assert report.deleted == 2
    assert set(remote_keys(h).values()) == {1}
    assert ("call", "user", "gone") not in remote_keys(h)


async def test_config_change_disables_and_hierarchy_change_deletes(harness: Harness) -> None:
    h = harness
    await h.connect_company()
    await h.services.reconciler.reconcile("c1")
    await h.services.targets.set_override("c1", "office", "c1-office-1", False)
    report = await h.services.reconciler.reconcile("c1")
    assert report.updated > 0
    assert all(not s.enabled for s in h.platform.subs["c1"].values())
    await h.services.targets.set_override("c1", "user", "c1-u1", True)  # per-target override wins
    await h.services.reconciler.reconcile("c1")
    enabled = {(s.kind, s.target_id) for s in h.platform.subs["c1"].values() if s.enabled}
    assert enabled == {("call", "c1-u1"), ("sms", "c1-u1")}

    company = h.platform.companies["c1"]
    company.targets = [t for t in company.targets if t.target_id != "c1-u2"]
    company.targets.append(
        TargetInfo(target_type="user", target_id="c1-new", parent_type="office", parent_id="c1-office-1")
    )
    await h.services.reconciler.reconcile("c1")
    keys = remote_keys(h)
    assert ("call", "user", "c1-u2") not in keys and keys[("call", "user", "c1-new")] == 1
    rec = await h.repo.get(Target.KIND, ("c1", "user", "c1-u2"))
    assert rec is not None and rec["deleted"] is True


def test_effective_enabled_inheritance() -> None:
    def T(tt, tid, pt=None, pid=None, o=None):
        return Target(
            company_id="c",
            target_type=tt,
            target_id=tid,
            parent_type=pt,
            parent_id=pid,
            override=o,
        )

    ts = [
        T("company", "c"),
        T("office", "o", "company", "c", False),
        T("user", "u", "office", "o"),
        T("user", "v", "office", "o", True),
        T("department", "d", "office", "missing"),
    ]
    eff = compute_effective(ts)
    assert eff == {
        ("company", "c"): True,
        ("office", "o"): False,
        ("user", "u"): False,
        ("user", "v"): True,
        ("department", "d"): True,
    }


async def test_weekly_sweep_is_staggered(harness: Harness) -> None:
    h = harness
    for i in range(50):
        await h.connect_company(f"co{i}", external=False)
    slots = {sweep_slot(f"co{i}") for i in range(50)}
    assert len(slots) > 45  # spread across the week
    sweeper = Sweeper(h.repo, h.queue, h.clock, tick_seconds=600)
    h.queue.published.clear()
    total = 0
    for _ in range(WEEK_SECONDS // 600):
        h.clock.advance(600)
        total += await sweeper.tick()
    assert total == 50  # every company exactly once per week
    assert due_in_window("x", 0, WEEK_SECONDS)


async def test_change_log_triggers_reconcile(harness: Harness) -> None:
    h = harness
    await h.connect_company()
    source = ScriptedChangeLogSource()
    watcher = ChangeLogWatcher(source, h.repo, h.queue, h.clock, ["target_created", "target_deleted"])
    h.queue.published.clear()
    source.events["c1"] = [ChangeEvent(action="call_ended")]
    assert await watcher.poll_company("c1") is False
    source.events["c1"] = [ChangeEvent(action="target_created", target_type="user", target_id="x")]
    assert await watcher.poll_company("c1") is True
    assert [m.job for m in h.queue.published] == ["reconcile"]


async def test_reconcile_job_runs_through_workers(harness: Harness) -> None:
    h = harness
    await h.connect_company()  # install enqueues a reconcile
    await h.drain()
    assert remote_keys(h)
