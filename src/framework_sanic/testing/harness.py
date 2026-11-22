"""In-process test harness: the full framework with in-memory adapters and fake Dialpad.

Use it to test features and connectors end to end without infrastructure::

    async def test_call_is_logged(harness: Harness) -> None:      # fixture from the pytest plugin
        await harness.connect_company()
        await harness.webhook("c1", call_event("42"))             # signed like Dialpad's
        await harness.drain()                                     # run queued jobs to completion
        assert harness.connector.records                          # inspect the fake CRM

Override the ``harness_connector`` / ``harness_settings`` fixtures to run it with your
connector or settings (see ``framework_sanic.testing.plugin``).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from framework_sanic.adapters.cache.memory import MemoryCache
from framework_sanic.adapters.queue.memory import MemoryQueue
from framework_sanic.adapters.storage.memory.repository import MemoryRepository
from framework_sanic.bootstrap import build_services, build_workers
from framework_sanic.core.config import Settings
from framework_sanic.core.container import Services
from framework_sanic.core.ports.platform import OAuthTokens, PlatformIdentity
from framework_sanic.http.app import HttpApp, build_http_app
from framework_sanic.http.routes import HttpRequest, HttpResponse
from framework_sanic.stubs.connector.connector import FakeConnector
from framework_sanic.stubs.dialpad import FakeDialpadPlatform, default_company
from framework_sanic.stubs.dialpad_tokens import make_dev_id_token
from framework_sanic.workers.runtime import WorkerRuntime

NOW_MS = int(time.time() * 1000)


class FakeClock:
    def __init__(self, start: float | None = None) -> None:
        self.t = start if start is not None else time.time()

    def now(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "_env_file": None,
        "PLATFORM_ADAPTER": "fake",
        "CONNECTOR": "fake",
        "STORAGE_ADAPTER": "memory",
        "QUEUE_ADAPTER": "memory",
        "CACHE_URL": "memory://",
        "SHARD_COUNT": 2,
        "PUBLIC_BASE_URL": "http://testserver",
        "UI_ORIGINS": "http://localhost:5173",
        "WORKER_CONCURRENCY": 16,
        "PER_COMPANY_CONCURRENCY": 4,
        "UI_RATE_LIMIT_PER_MINUTE": 60,
        "FEATURES": "call_logging,sms_logging",
    }
    base.update(overrides)
    return Settings(**base)


@dataclass
class Harness:
    services: Services
    http: HttpApp
    workers: WorkerRuntime
    platform: FakeDialpadPlatform
    connector: Any  # FakeConnector unless an integration passes its own
    repo: Any
    queue: MemoryQueue
    cache: MemoryCache
    clock: FakeClock

    @property
    def secret(self) -> str:
        return self.services.settings.DIALPAD_CLIENT_SECRET.get_secret_value()

    def id_token(self, company_id: str = "c1", user_id: str = "c1-u0", is_admin: bool = True) -> str:
        return make_dev_id_token(self.secret, company_id=company_id, user_id=user_id, is_admin=is_admin)

    async def connect_company(
        self, company_id: str = "c1", *, external: bool = True, admin: str | None = None
    ) -> None:
        self.platform.add_company(default_company(company_id))
        identity = PlatformIdentity(
            company_id=company_id, company_name="Acme", user_id=admin or f"{company_id}-u0", is_admin=True
        )
        tokens = self.platform.issue_tokens(identity)
        await self.services.companies.install(identity, tokens)
        if external:
            await self.services.external_credentials.save(
                company_id,
                "company",
                "",
                OAuthTokens(access_token="ext-company", refresh_token="r", expires_in=3600),
            )

    async def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        body: bytes | str | None = None,
        headers: dict[str, str] | None = None,
    ) -> HttpResponse:
        raw = body.encode() if isinstance(body, str) else (body or b"")
        return await self.http.handle(
            HttpRequest(
                method=method,
                path=path,
                headers={k.lower(): v for k, v in (headers or {}).items()},
                query=query or {},
                body=raw,
            )
        )

    def sign(self, company_id: str, payload: dict[str, Any]) -> str:
        return self.services.webhooks.sign(company_id, payload)  # type: ignore[attr-defined, no-any-return]

    async def webhook(self, company_id: str, payload: dict[str, Any]) -> HttpResponse:
        return await self.request(
            "POST", f"/webhooks/dialpad/{company_id}/events", body=self.sign(company_id, payload)
        )

    async def drain(self) -> int:
        return await self.workers.drain()


def build_harness(
    *,
    repo: Any = None,
    connector: Any = None,
    settings: Settings | None = None,
    **setting_overrides: Any,
) -> Harness:
    """The whole framework in one process: memory adapters, a fake Dialpad, your connector (the
    fake CRM by default) and every enabled feature. Nothing leaves the process."""
    clock = FakeClock()
    settings = settings or make_settings(**setting_overrides)
    repo = repo or MemoryRepository(clock)
    queue = MemoryQueue(settings.SHARD_COUNT)
    cache = MemoryCache()
    platform = FakeDialpadPlatform()
    connector = connector if connector is not None else FakeConnector()
    services = build_services(
        settings, clock=clock, repo=repo, queue=queue, cache=cache, platform=platform, connector=connector
    )
    return Harness(
        services=services,
        http=build_http_app(services),
        workers=build_workers(services),
        platform=platform,
        connector=connector,
        repo=repo,
        queue=queue,
        cache=cache,
        clock=clock,
    )


def call_event(
    call_id: str = "1001",
    state: str = "hangup",
    ts: int | None = None,
    *,
    target: dict[str, Any] | None = None,
    entry_point_target: dict[str, Any] | None = None,
    external_number: str = "+14155550100",
    **extra: Any,
) -> dict[str, Any]:
    ts = NOW_MS if ts is None else ts
    payload: dict[str, Any] = {
        "call_id": int(call_id) if call_id.isdigit() else call_id,
        "state": state,
        "event_timestamp": ts,
        "direction": "inbound",
        "external_number": external_number,
        "internal_number": "+14155550199",
        "target": target or {"id": "c1-u1", "type": "user", "name": "User 1"},
        "contact": {"id": None, "phone": external_number, "name": "Caller"},
        "date_started": ts - 60_000,
    }
    if entry_point_target:
        payload["entry_point_target"] = entry_point_target
    if state in ("hangup", "missed", "voicemail"):
        payload.update(date_ended=ts, duration=60_000.0)
    payload.update(extra)
    return payload


def sms_event(
    sms_id: int = 555, text: str = "hello there", status: str = "sent", **extra: Any
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": sms_id,
        "created_date": NOW_MS,
        "direction": "inbound",
        "from_number": "+14155550100",
        "to_number": ["+14155550199"],
        "target": {"id": "c1-u1", "type": "user"},
        "contact": {"phone": "+14155550100"},
        "message_status": status,
        "text": text,
    }
    payload.update(extra)
    return payload
