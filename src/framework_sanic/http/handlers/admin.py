"""Admin endpoints: logging configuration per target, reconcile, uninstall."""

from __future__ import annotations

import logging

from pydantic import BaseModel

from framework_sanic.core.domain.errors import NotFound
from framework_sanic.core.domain.records import Lane
from framework_sanic.core.services.subscription_reconciler import reconcile_message

from ..routes import RequestContext, route

log = logging.getLogger(__name__)


class Empty(BaseModel):
    pass


class TargetOut(BaseModel):
    target_type: str
    target_id: str
    name: str
    parent_type: str | None
    parent_id: str | None
    override: bool | None
    effective_enabled: bool


class TargetsOut(BaseModel):
    targets: list[TargetOut]
    notice: str


class LoggingIn(BaseModel):
    target_type: str
    target_id: str
    override: bool | None  # null = inherit from parent


class Queued(BaseModel):
    queued: bool = True


class UninstallOut(BaseModel):
    records_deleted: int
    subscriptions_removed: int


@route("GET", "/admin/targets", auth="admin")
async def list_targets(req: Empty, ctx: RequestContext) -> TargetsOut:
    from framework_sanic.core.services.company_service import LOGGING_NOTICE

    principal = ctx.require_principal()
    targets = await ctx.services.targets.all(principal.company_id)
    return TargetsOut(
        targets=[TargetOut(**t.model_dump()) for t in sorted(targets, key=lambda t: (t.target_type, t.name))],
        notice=LOGGING_NOTICE,
    )


@route("PUT", "/admin/targets/{target_type}/{target_id}/logging", auth="admin")
async def set_logging(req: LoggingIn, ctx: RequestContext) -> TargetOut:
    s = ctx.services
    principal = ctx.require_principal()
    if await s.targets.get(principal.company_id, req.target_type, req.target_id) is None:
        raise NotFound("unknown target")
    target = await s.targets.set_override(principal.company_id, req.target_type, req.target_id, req.override)
    await s.companies.audit(
        principal.company_id,
        "config_change",
        principal.user_id,
        {"target_type": req.target_type, "target_id": req.target_id, "override": req.override},
    )
    await s.queue.publish(
        reconcile_message(principal.company_id, lane=Lane.UI, reason="config-change", now=s.clock.now())
    )
    return TargetOut(**target.model_dump())


@route("POST", "/admin/reconcile", auth="admin", status=202)
async def reconcile(req: Empty, ctx: RequestContext) -> Queued:
    s = ctx.services
    principal = ctx.require_principal()
    await s.queue.publish(
        reconcile_message(principal.company_id, lane=Lane.UI, reason="admin-request", now=s.clock.now())
    )
    return Queued()


@route("POST", "/admin/uninstall", auth="admin")
async def uninstall(req: Empty, ctx: RequestContext) -> UninstallOut:
    """Delete all company data (the audit log is kept). Remote subscriptions are removed first."""
    s = ctx.services
    principal = ctx.require_principal()
    removed = 0
    try:
        removed = await s.reconciler.remove_all(principal.company_id)
    except Exception:  # noqa: BLE001 - deletion of local data must proceed regardless
        log.exception("could not remove Dialpad subscriptions for %s", principal.company_id)
    deleted = await s.companies.uninstall(
        principal.company_id, principal.user_id, s.features.company_data if s.features else ()
    )
    return UninstallOut(records_deleted=deleted, subscriptions_removed=removed)
