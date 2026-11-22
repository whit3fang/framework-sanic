"""Company lifecycle: install (admin connect), app state, parking, uninstall, audit log."""

from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel

from ..domain.entities import ALL_ENTITIES, AuditEvent, Company, Entity, User
from ..domain.records import Lane, Record, stable_hash
from ..ports.platform import OAuthTokens, PlatformIdentity
from ..ports.queue import Queue
from ..ports.repository import Repository
from ..ports.runtime import Clock
from .credential_service import DialpadCredentialService, ExternalCredentialService
from .session_service import Principal
from .subscription_reconciler import reconcile_message

log = logging.getLogger(__name__)

AppState = Literal["needs_admin_connect", "awaiting_admin", "needs_external_connect", "ready", "forbidden"]

LOGGING_NOTICE = (
    "Call and SMS logging is on by default for every user, department and call center. "
    "Admins can turn it off per target."
)


class AppStatus(BaseModel):
    state: AppState
    company_id: str
    user_id: str
    is_admin: bool
    external: dict[str, bool]
    admin_notice: str | None = None


class CompanyService:
    def __init__(
        self,
        repo: Repository,
        queue: Queue,
        clock: Clock,
        dialpad_credentials: DialpadCredentialService,
        external_credentials: ExternalCredentialService,
    ) -> None:
        self.repo = repo
        self.queue = queue
        self.clock = clock
        self.dialpad_credentials = dialpad_credentials
        self.external_credentials = external_credentials

    async def get(self, company_id: str) -> Company | None:
        rec = await self.repo.get(Company.KIND, (company_id,))
        return Company.from_record(rec) if rec else None

    async def audit(
        self, company_id: str, action: str, actor: str, details: dict[str, Any] | None = None
    ) -> None:
        now = self.clock.now()
        event = AuditEvent(
            company_id=company_id,
            event_id=f"{int(now * 1000):015d}-{stable_hash(action, actor, details, now, length=10)}",
            action=action,
            actor=actor,
            details=details or {},
            at=now,
        )
        await self.repo.upsert(AuditEvent.KIND, event.key(), lambda cur: cur or event.to_record())

    async def install(self, identity: PlatformIdentity, tokens: OAuthTokens) -> Company:
        now = self.clock.now()

        def merge(cur: Record | None) -> Record | None:
            base = (
                Company.from_record(cur) if cur else Company(company_id=identity.company_id, installed_at=now)
            )
            return base.model_copy(
                update={
                    "name": identity.company_name or base.name,
                    "connection_state": "ok",
                    "parked_reason": None,
                    "installed_at": base.installed_at or now,
                }
            ).to_record()

        await self.repo.upsert(Company.KIND, (identity.company_id,), merge)
        await self.dialpad_credentials.connect(identity, tokens)
        await self.repo.upsert(
            User.KIND,
            (identity.company_id, identity.user_id),
            lambda cur: {
                **(cur or User(company_id=identity.company_id, dialpad_user_id=identity.user_id).to_record()),
                "name": identity.name,
                "email": identity.email,
                "is_admin": identity.is_admin,
            },
        )
        await self.audit(
            identity.company_id, "connect", identity.user_id, {"credential_id": identity.user_id}
        )
        await self.queue.publish(
            reconcile_message(identity.company_id, lane=Lane.UI, reason="admin-connect", now=now)
        )
        company = await self.get(identity.company_id)
        assert company is not None
        return company

    async def park(self, company_id: str, reason: str) -> None:
        """Invalid or revoked connection: park stage-2 sync (stage 1 keeps flowing) and alert."""

        def merge(cur: Record | None) -> Record | None:
            if not cur or cur.get("connection_state") == "invalid":
                return cur
            return {**cur, "connection_state": "invalid", "parked_reason": reason}

        if await self.repo.upsert(Company.KIND, (company_id,), merge) == "updated":
            log.error("ALERT company %s parked: %s", company_id, reason)

    async def unpark(self, company_id: str) -> None:
        await self.repo.upsert(
            Company.KIND,
            (company_id,),
            lambda cur: (
                {**cur, "connection_state": "ok", "parked_reason": None}
                if cur and cur.get("connection_state") == "invalid"
                else cur
            ),
        )

    async def status(self, principal: Principal) -> AppStatus:
        company = await self.get(principal.company_id)
        external = await self.external_credentials.status(principal.company_id, principal.user_id)
        is_admin = principal.is_admin
        connected = (
            company is not None
            and company.connection_state != "uninstalled"
            and await self.dialpad_credentials.has_usable(principal.company_id)
        )
        state: AppState
        if not connected:
            state = "needs_admin_connect" if is_admin else "awaiting_admin"
        elif not (external["user"] or external["company"]):
            state = "needs_external_connect"
        else:
            state = "ready"
        return AppStatus(
            state=state,
            company_id=principal.company_id,
            user_id=principal.user_id,
            is_admin=is_admin,
            external=external,
            admin_notice=LOGGING_NOTICE if is_admin else None,
        )

    async def uninstall(
        self, company_id: str, actor: str, feature_entities: tuple[type[Entity], ...] = ()
    ) -> int:
        """Delete all company data: framework kinds plus the kinds enabled features declared
        (``ctx.declare_company_data``). The audit log is kept."""
        deleted = 0
        entities = [e for e in ALL_ENTITIES if e is not AuditEvent] + list(feature_entities)
        for cls in entities:
            keys: list[tuple[str | int, ...]] = []
            async for rec in self.repo.query(cls.KIND, company_id=company_id):
                keys.append(cls.from_record(rec).key())
            kind = cls.KIND
            for key in keys:
                await self.repo.delete(kind, key)
                deleted += 1
        await self.audit(company_id, "delete", actor, {"records_deleted": deleted})
        return deleted
