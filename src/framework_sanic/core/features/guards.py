"""Guards for jobs that call the external service (framework toolkit, usable by any integration).

``SyncGuards.check`` runs, in order: company parked/uninstalled -> external credential
(user-first, then company) -> per-connector, per-company rate budget for the message's lane.
Waiting because of parking or rate limits never counts as a failed attempt.

``lease``/``release`` give a short best-effort exclusive section (a fast path only: correctness
must come from idempotent writes). ``on_error`` maps connector errors to retry/deadletter.
"""

from __future__ import annotations

import logging
import random
from typing import TYPE_CHECKING

from ..domain.entities import Company
from ..domain.errors import NotConnected
from ..ports.connector import ConnectorCredential, ConnectorError
from ..ports.queue import QueueMessage
from ..services.lease import acquire_lease, release_lease
from .jobs import JobResult, backoff, deadletter, retry

if TYPE_CHECKING:
    from ..container import Services

log = logging.getLogger(__name__)

PARK_DELAY_SECONDS = 300.0
LEASE_SECONDS = 60.0


class SyncGuards:
    def __init__(self, services: Services) -> None:
        self.s = services

    async def parked(self, company_id: str) -> JobResult | None:
        rec = await self.s.repo.get(Company.KIND, (company_id,))
        if rec is None:
            return deadletter("unknown company")
        company = Company.from_record(rec)
        if company.connection_state == "uninstalled":
            return deadletter("company uninstalled")
        if company.connection_state == "invalid":
            return retry(PARK_DELAY_SECONDS, f"parked: {company.parked_reason}", count_attempt=False)
        return None

    async def rate(self, msg: QueueMessage) -> JobResult | None:
        wait = await self.s.limiter.acquire(
            f"connector:{self.s.connector.name}:{msg.company_id}",
            per_minute=self.s.settings.CONNECTOR_RATE_LIMIT_PER_MINUTE,
            lane=msg.lane,
        )
        if wait > 0:
            return retry(wait + random.uniform(0, 0.5), "rate limited", count_attempt=False)
        return None

    async def credential(self, company_id: str, user_id: str | None) -> ConnectorCredential | JobResult:
        try:
            return await self.s.external_credentials.resolve(company_id, user_id)
        except NotConnected:
            log.error("ALERT no usable %s credential for %s; parking sync", self.s.connector.name, company_id)
            return retry(PARK_DELAY_SECONDS, "no external credential", count_attempt=False)

    async def check(self, msg: QueueMessage, user_id: str | None) -> ConnectorCredential | JobResult:
        """A credential when the job may call the external service now, else the JobResult to return."""
        parked = await self.parked(msg.company_id)
        if parked:
            return parked
        cred = await self.credential(msg.company_id, user_id)
        if isinstance(cred, JobResult):
            return cred
        limited = await self.rate(msg)
        return limited or cred

    async def lease(self, key: str, seconds: float = LEASE_SECONDS) -> str | None:
        """A token while this job may sync ``key`` (pass it to ``release``), else None."""
        return await acquire_lease(self.s.cache, f"lease:sync:{key}", seconds)

    async def release(self, key: str, token: str) -> None:
        """Frees the lease only if it is still this job's (it may have expired and moved on)."""
        await release_lease(self.s.cache, f"lease:sync:{key}", token)

    async def on_error(self, exc: Exception, msg: QueueMessage, cred: ConnectorCredential) -> JobResult:
        err = exc if isinstance(exc, ConnectorError) else self.s.connector.map_error(exc)
        if err.kind == "auth":
            await self.s.external_credentials.mark_bad(msg.company_id, cred.scope, cred.user_id)
            return retry(0.1, "external auth failed; trying next credential", count_attempt=False)
        if err.kind == "rate_limited":
            ra = err.retry_after or 10.0
            return retry(ra + random.uniform(0, ra / 2), "external 429", count_attempt=False)
        if err.kind == "transient":
            return retry(backoff(msg.attempt), f"transient: {err}")
        log.error("permanent connector error for %s: %s", msg.job, err)
        return deadletter(f"{err.kind}: {err}")
