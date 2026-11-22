"""Subscription reconciler: diff desired per-target subscriptions against Dialpad.

* Subscriptions are per target (a company-level subscription does not cover users' calls).
* Lists existing subscriptions, diffs against desired state: create, update, enable/disable,
  delete duplicates and orphans. Idempotent and safe to rerun or run concurrently:
  creation is claimed through an atomic repository upsert (correctness), and a Redis lease
  serializes runs per company (fast path only).
* Subscriptions are never auto-disabled on delivery failures.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Literal

from ..domain.entities import Company, Subscription, Target
from ..domain.records import Lane, Record, stable_hash
from ..ports.cache import Cache, CacheUnavailable
from ..ports.platform import RemoteSubscription, SubscriptionSpec
from ..ports.queue import TOPIC_CONTROL, Queue, QueueMessage
from ..ports.repository import Repository
from ..ports.runtime import ChangeLogSource, Clock
from ..ports.security import WebhookVerifier
from .credential_service import PlatformGateway
from .target_hierarchy import TargetService

log = logging.getLogger(__name__)

SubKind = Literal["call", "sms"]
CLAIM_FRESH_SECONDS = 300.0
WEEK_SECONDS = 7 * 24 * 3600


@dataclass
class ReconcileReport:
    created: int = 0
    updated: int = 0
    deleted: int = 0
    unchanged: int = 0
    skipped_claims: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ReconcilerConfig:
    loggable_target_types: tuple[str, ...]
    call_states: tuple[str, ...]
    subscription_kinds: tuple[SubKind, ...]
    hook_url: Callable[[str], str]


def _matches(remote: RemoteSubscription, spec: SubscriptionSpec) -> bool:
    if remote.enabled != spec.enabled:
        return False
    s = remote.settings
    if spec.kind == "call":
        return (
            sorted(s.get("call_states") or []) == sorted(spec.call_states)
            and bool(s.get("group_calls_only", False)) == spec.group_calls_only
        )
    return (
        s.get("direction", "all") == spec.direction
        and bool(s.get("include_internal", False)) == spec.include_internal
        and bool(s.get("status", False)) == spec.status
    )


class SubscriptionReconciler:
    def __init__(
        self,
        repo: Repository,
        gateway: PlatformGateway,
        targets: TargetService,
        webhooks: WebhookVerifier,
        cache: Cache,
        clock: Clock,
        config: ReconcilerConfig,
    ) -> None:
        self.repo = repo
        self.gateway = gateway
        self.platform = gateway.platform
        self.targets = targets
        self.webhooks = webhooks
        self.cache = cache
        self.clock = clock
        self.config = config

    async def _lease(self, company_id: str) -> bool:
        try:
            return await self.cache.add(f"lease:reconcile:{company_id}", b"1", 300)
        except CacheUnavailable:
            return True

    async def _release(self, company_id: str) -> None:
        try:
            await self.cache.delete(f"lease:reconcile:{company_id}")
        except CacheUnavailable:
            pass

    def desired(
        self, targets: Iterable[Target], endpoint_id: str
    ) -> dict[tuple[SubKind, str, str], SubscriptionSpec]:
        out: dict[tuple[SubKind, str, str], SubscriptionSpec] = {}
        for t in targets:
            if t.deleted or t.target_type not in self.config.loggable_target_types:
                continue
            for kind in self.config.subscription_kinds:
                if kind == "call":
                    spec = SubscriptionSpec(
                        kind="call",
                        target_type=t.target_type,
                        target_id=t.target_id,
                        endpoint_id=endpoint_id,
                        enabled=t.effective_enabled,
                        call_states=list(self.config.call_states),
                        group_calls_only=False,
                    )
                else:
                    # VERIFY: contact_group (group SMS) policy.
                    spec = SubscriptionSpec(
                        kind="sms",
                        target_type=t.target_type,
                        target_id=t.target_id,
                        endpoint_id=endpoint_id,
                        enabled=t.effective_enabled,
                        direction="all",
                        include_internal=False,
                        status=False,
                    )
                out[(kind, t.target_type, t.target_id)] = spec
        return out

    async def reconcile(
        self, company_id: str, *, lane: Lane = Lane.BACKFILL, use_lease: bool = True
    ) -> ReconcileReport:
        report = ReconcileReport()
        if use_lease and not await self._lease(company_id):
            log.info("reconcile for %s already running; skipping", company_id)
            return report
        try:
            await self._reconcile(company_id, lane, report)
        finally:
            if use_lease:
                await self._release(company_id)
        return report

    async def _reconcile(self, company_id: str, lane: Lane, report: ReconcileReport) -> None:
        infos = await self.gateway.call(
            company_id, lambda t: self.platform.list_targets(t, company_id), lane=lane
        )
        await self.targets.sync_from_platform(company_id, infos)
        hook_url = self.config.hook_url(company_id)
        secret = self.webhooks.secret_for(company_id)
        endpoint_id = await self.gateway.call(
            company_id, lambda t: self.platform.ensure_webhook(t, hook_url=hook_url, secret=secret), lane=lane
        )
        await self.repo.upsert(
            Company.KIND,
            (company_id,),
            lambda cur: {**cur, "webhook_endpoint_id": endpoint_id} if cur else None,
        )
        desired = self.desired(await self.targets.all(company_id), endpoint_id)

        for kind in self.config.subscription_kinds:
            remote_all = await self._list_remote(company_id, kind, lane)
            remote = [r for r in remote_all if r.endpoint_id in (None, endpoint_id)]
            groups: dict[tuple[str, str], list[RemoteSubscription]] = defaultdict(list)
            for r in remote:
                groups[(r.target_type or "", r.target_id or "")].append(r)

            for (ttype, tid), subs in groups.items():
                subs.sort(key=lambda r: r.id)
                keep, extras = subs[0], subs[1:]
                for extra in extras:  # duplicates (e.g. from an older buggy run): delete
                    await self._delete_remote(company_id, kind, extra.id, lane)
                    report.deleted += 1
                spec = desired.get((kind, ttype, tid))
                if spec is None:
                    await self._delete_remote(company_id, kind, keep.id, lane)
                    await self.repo.delete(Subscription.KIND, (company_id, ttype, tid, kind))
                    report.deleted += 1
                    continue
                if not _matches(keep, spec):
                    await self._update_remote(company_id, keep.id, spec, lane)
                    report.updated += 1
                else:
                    report.unchanged += 1
                await self._mark_active(company_id, spec, keep.id)

            for (k, ttype, tid), spec in desired.items():
                if k != kind or (ttype, tid) in groups:
                    continue
                if not await self._claim(company_id, spec):
                    report.skipped_claims += 1
                    continue
                created = await self._create_remote(company_id, spec, lane)
                await self._mark_active(company_id, spec, created.id)
                report.created += 1

    async def remove_all(self, company_id: str, *, lane: Lane = Lane.UI) -> int:
        """Delete every subscription created by this app (used on uninstall)."""
        company = await self.repo.get(Company.KIND, (company_id,))
        endpoint_id = company.get("webhook_endpoint_id") if company else None
        removed = 0
        for kind in self.config.subscription_kinds:
            for r in await self._list_remote(company_id, kind, lane):
                if endpoint_id is None or r.endpoint_id in (None, endpoint_id):
                    await self._delete_remote(company_id, kind, r.id, lane)
                    removed += 1
        return removed

    async def _list_remote(self, company_id: str, kind: SubKind, lane: Lane) -> list[RemoteSubscription]:
        return await self.gateway.call(
            company_id, lambda t: self.platform.list_subscriptions(t, kind), lane=lane
        )

    async def _update_remote(self, company_id: str, sub_id: str, spec: SubscriptionSpec, lane: Lane) -> None:
        await self.gateway.call(
            company_id, lambda t: self.platform.update_subscription(t, sub_id, spec), lane=lane
        )

    async def _create_remote(self, company_id: str, spec: SubscriptionSpec, lane: Lane) -> RemoteSubscription:
        return await self.gateway.call(
            company_id, lambda t: self.platform.create_subscription(t, spec), lane=lane
        )

    async def _delete_remote(self, company_id: str, kind: SubKind, sub_id: str, lane: Lane) -> None:
        await self.gateway.call(
            company_id, lambda t: self.platform.delete_subscription(t, kind, sub_id), lane=lane
        )

    async def _claim(self, company_id: str, spec: SubscriptionSpec) -> bool:
        """Atomically claim the right to create this subscription. Losers skip."""
        now = self.clock.now()
        key = (company_id, spec.target_type, spec.target_id, spec.kind)

        def merge(cur: Record | None) -> Record | None:
            if cur is not None and now - float(cur.get("claimed_at", 0.0)) < CLAIM_FRESH_SECONDS:
                return None  # being created right now, or created moments ago by a concurrent run
            return Subscription(
                company_id=company_id,
                target_type=spec.target_type,
                target_id=spec.target_id,
                kind=spec.kind,
                enabled=spec.enabled,
                status="creating",
                claimed_at=now,
            ).to_record()

        return await self.repo.upsert(Subscription.KIND, key, merge) in ("created", "updated")

    async def _mark_active(self, company_id: str, spec: SubscriptionSpec, sub_id: str) -> None:
        key = (company_id, spec.target_type, spec.target_id, spec.kind)
        now = self.clock.now()

        def merge(cur: Record | None) -> Record | None:
            base = Subscription.from_record(cur) if cur else None
            if (
                base
                and base.dialpad_subscription_id == sub_id
                and base.enabled == spec.enabled
                and base.status == "active"
            ):
                return cur
            return Subscription(
                company_id=company_id,
                target_type=spec.target_type,
                target_id=spec.target_id,
                kind=spec.kind,
                dialpad_subscription_id=sub_id,
                enabled=spec.enabled,
                status="active",
                claimed_at=base.claimed_at if base and base.dialpad_subscription_id == sub_id else now,
            ).to_record()

        await self.repo.upsert(Subscription.KIND, key, merge)


def reconcile_message(company_id: str, *, lane: Lane, reason: str, now: float) -> QueueMessage:
    return QueueMessage(
        message_id=f"reconcile:{company_id}:{stable_hash(reason, int(now), length=12)}",
        topic=TOPIC_CONTROL,
        company_id=company_id,
        lane=lane,
        job="reconcile",
        payload={"reason": reason},
        enqueued_at=now,
    )


def sweep_slot(company_id: str, period_seconds: int = WEEK_SECONDS) -> int:
    """Each company gets a stable, jittered slot in the week so sweeps are staggered."""
    return int(stable_hash("sweep", company_id, length=12), 16) % period_seconds


def due_in_window(
    company_id: str, window_start: float, window_end: float, period_seconds: int = WEEK_SECONDS
) -> bool:
    if window_end - window_start >= period_seconds:
        return True
    slot = sweep_slot(company_id, period_seconds)
    start = int(window_start) % period_seconds
    end = int(window_end) % period_seconds
    if start <= end:
        return start <= slot < end
    return slot >= start or slot < end


class Sweeper:
    """Weekly staggered sweep: on every tick, enqueue reconciles for companies whose slot passed."""

    def __init__(self, repo: Repository, queue: Queue, clock: Clock, tick_seconds: float) -> None:
        self.repo = repo
        self.queue = queue
        self.clock = clock
        self.tick_seconds = tick_seconds
        self._last: float | None = None

    async def tick(self) -> int:
        now = self.clock.now()
        start = self._last if self._last is not None else now - self.tick_seconds
        self._last = now
        count = 0
        async for rec in self.repo.query(Company.KIND):
            company = Company.from_record(rec)
            if company.connection_state == "uninstalled":
                continue
            if due_in_window(company.company_id, start, now):
                await self.queue.publish(
                    reconcile_message(company.company_id, lane=Lane.BACKFILL, reason="weekly-sweep", now=now)
                )
                count += 1
        return count


class ChangeLogWatcher:
    """Change-log events are refresh triggers only: target created/deleted -> enqueue a reconcile."""

    def __init__(
        self,
        source: ChangeLogSource,
        repo: Repository,
        queue: Queue,
        clock: Clock,
        lifecycle_actions: Iterable[str],
    ) -> None:
        self.source = source
        self.repo = repo
        self.queue = queue
        self.clock = clock
        self.actions = set(lifecycle_actions)
        self._cursors: dict[str, str | None] = {}

    async def poll_company(self, company_id: str) -> bool:
        events, cursor = await self.source.poll(company_id, self._cursors.get(company_id))
        self._cursors[company_id] = cursor
        if any(e.action in self.actions for e in events):
            await self.queue.publish(
                reconcile_message(company_id, lane=Lane.BACKFILL, reason="change-log", now=self.clock.now())
            )
            return True
        return False

    async def poll_all(self) -> None:
        async for rec in self.repo.query(Company.KIND, connection_state="ok"):
            await self.poll_company(str(rec["company_id"]))
