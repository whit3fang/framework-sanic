"""Composition root: turns Settings into adapters, services, features, workers and schedules.

Adapters are swappable by config (STORAGE_ADAPTER, QUEUE_ADAPTER, CACHE_URL, KMS_KEY, CONNECTOR,
PLATFORM_ADAPTER). Tests pass explicit adapter instances through ``overrides``.
"""

from __future__ import annotations

import dataclasses
import importlib
import logging
from typing import Any

from framework_sanic.adapters.changelog.noop import NoopChangeLogSource
from framework_sanic.adapters.dialpad.id_token import DialpadIdTokenVerifier
from framework_sanic.adapters.dialpad.webhook import DialpadWebhookVerifier
from framework_sanic.adapters.scheduler.memory import AsyncioScheduler
from framework_sanic.adapters.storage.token_store import RepositoryTokenStore
from framework_sanic.core.config import Settings
from framework_sanic.core.container import Services
from framework_sanic.core.domain.entities import ALL_ENTITIES
from framework_sanic.core.domain.errors import ConfigurationError
from framework_sanic.core.features.context import load_features
from framework_sanic.core.ports.connector import Connector
from framework_sanic.core.ports.queue import TOPIC_CONTROL, TOPIC_DEADLETTER, TOPIC_EVENTS, TOPIC_SYNC
from framework_sanic.core.ports.repository import IndexSpec, Repository, StorageSchema
from framework_sanic.core.ports.runtime import SystemClock
from framework_sanic.core.services.crypto import EnvelopeCipher
from framework_sanic.workers.runtime import WorkerPool, WorkerRuntime

log = logging.getLogger(__name__)


BUILTIN_STORAGE = ("memory", "sqlalchemy", "datastore")


def make_repository(settings: Settings, clock: Any) -> Repository:
    """Built-in adapters by name, or ``package.module:factory`` -> ``factory(settings, clock)``."""
    name = settings.STORAGE_ADAPTER
    if ":" in name:
        module_name, _, attr = name.partition(":")
        try:
            factory = getattr(importlib.import_module(module_name), attr)
        except (ImportError, AttributeError) as exc:
            raise ConfigurationError(f"STORAGE_ADAPTER {name!r} cannot be loaded: {exc}") from exc
        repo = factory(settings, clock)
        if not isinstance(repo, Repository):
            raise ConfigurationError(
                f"STORAGE_ADAPTER {name!r} returned {type(repo).__name__}, which does not implement "
                "the Repository port (core/ports/repository.py)"
            )
        return repo
    if name not in BUILTIN_STORAGE:
        raise ConfigurationError(
            f"unknown STORAGE_ADAPTER {name!r}: use one of {BUILTIN_STORAGE} or 'package.module:factory'"
        )
    if name == "memory":
        from framework_sanic.adapters.storage.memory.repository import MemoryRepository

        return MemoryRepository(clock)
    if name == "sqlalchemy":
        from framework_sanic.adapters.storage.sqlalchemy.repository import SqlAlchemyRepository

        return SqlAlchemyRepository(settings.DATABASE_URL, clock=clock)
    from framework_sanic.adapters.storage.datastore.repository import DatastoreRepository

    return DatastoreRepository(
        settings.DATASTORE_PROJECT, namespace=settings.DATASTORE_NAMESPACE, clock=clock
    )


def make_queue(settings: Settings) -> Any:
    if settings.QUEUE_ADAPTER == "memory":
        from framework_sanic.adapters.queue.memory import MemoryQueue

        return MemoryQueue(settings.SHARD_COUNT)
    from framework_sanic.adapters.queue.pubsub import PubSubQueue

    return PubSubQueue(
        settings.PUBSUB_PROJECT,
        prefix=settings.PUBSUB_PREFIX,
        shard_count=settings.SHARD_COUNT,
        topics=[TOPIC_EVENTS, TOPIC_SYNC, TOPIC_CONTROL, TOPIC_DEADLETTER],
    )


def make_cache(settings: Settings) -> Any:
    if settings.CACHE_URL.startswith("memory://"):
        from framework_sanic.adapters.cache.memory import MemoryCache

        return MemoryCache()
    from framework_sanic.adapters.cache.redis import RedisCache

    return RedisCache(settings.CACHE_URL)


def make_key_provider(settings: Settings) -> Any:
    if settings.KMS_KEY:
        from framework_sanic.adapters.kms.gcp_kms import GcpKmsKeyProvider

        return GcpKmsKeyProvider(settings.KMS_KEY)
    from framework_sanic.adapters.kms.local import LocalKeyProvider

    return LocalKeyProvider(settings.LOCAL_MASTER_KEY.get_secret_value())


def make_platform(settings: Settings) -> Any:
    if settings.PLATFORM_ADAPTER == "fake":
        from framework_sanic.stubs.dialpad import FakeDialpadPlatform

        return FakeDialpadPlatform()
    from framework_sanic.adapters.dialpad.platform import DialpadPlatform

    return DialpadPlatform(
        base_url=settings.DIALPAD_API_BASE,
        client_id=settings.DIALPAD_CLIENT_ID,
        client_secret=settings.DIALPAD_CLIENT_SECRET.get_secret_value(),
        scopes=settings.dialpad_scopes,
    )


def make_connector(settings: Settings) -> Connector:
    if settings.CONNECTOR == "fake":
        from framework_sanic.stubs.connector.connector import FakeConnector

        return FakeConnector()
    if settings.CONNECTOR == "hubspot":
        from framework_sanic.connectors.hubspot.connector import HubSpotConnector

        return HubSpotConnector(
            client_id=settings.HUBSPOT_CLIENT_ID,
            client_secret=settings.HUBSPOT_CLIENT_SECRET.get_secret_value(),
            api_base=settings.HUBSPOT_API_BASE,
            portal_id=settings.HUBSPOT_PORTAL_ID,
        )
    raise ConfigurationError(f"unknown CONNECTOR {settings.CONNECTOR!r}")


def build_services(settings: Settings | None = None, **overrides: Any) -> Services:
    settings = settings or Settings()
    clock = overrides.pop("clock", None) or SystemClock()
    repo = overrides.pop("repo", None) or make_repository(settings, clock)
    cache = overrides.pop("cache", None) or make_cache(settings)
    queue = overrides.pop("queue", None) or make_queue(settings)
    key_provider = overrides.pop("key_provider", None) or make_key_provider(settings)
    cipher = EnvelopeCipher(key_provider)
    services = Services.build(
        settings=settings,
        clock=clock,
        repo=repo,
        queue=queue,
        cache=cache,
        platform=overrides.pop("platform", None) or make_platform(settings),
        connector=overrides.pop("connector", None) or make_connector(settings),
        key_provider=key_provider,
        token_store=RepositoryTokenStore(repo, cipher, clock),
        id_tokens=overrides.pop("id_tokens", None)
        or DialpadIdTokenVerifier(settings.DIALPAD_CLIENT_SECRET.get_secret_value()),
        webhooks=DialpadWebhookVerifier(settings.WEBHOOK_SECRET_KEY.get_secret_value()),
        changelog=overrides.pop("changelog", None) or NoopChangeLogSource(),
        scheduler=overrides.pop("scheduler", None) or AsyncioScheduler(),
        cipher=cipher,
    )
    if overrides:
        raise TypeError(f"unknown overrides: {sorted(overrides)}")
    registry = load_features(services, settings.features)
    services.features = registry
    services.reconciler.config = dataclasses.replace(
        services.reconciler.config, subscription_kinds=registry.subscription_kinds
    )
    return services


def build_workers(services: Services, *, shards: list[int] | None = None) -> WorkerRuntime:
    assert services.features is not None
    shards = shards if shards is not None else list(range(services.settings.SHARD_COUNT))
    by_topic: dict[str, dict[str, Any]] = {}
    for (topic, job), handler in services.features.jobs.items():
        by_topic.setdefault(topic, {})[job] = handler
    s = services.settings
    pools = [
        WorkerPool(
            services.queue,
            topic,
            handlers,
            shards=shards,
            concurrency=s.WORKER_CONCURRENCY,
            per_company_cap=s.PER_COMPANY_CONCURRENCY,
        )
        for topic, handlers in sorted(by_topic.items())
    ]
    return WorkerRuntime(pools)


def storage_schema(services: Services) -> StorageSchema:
    """Every kind the framework and enabled features store, plus their declared indexes."""
    kinds = [e.KIND for e in ALL_ENTITIES]
    indexes: list[IndexSpec] = []
    if services.features is not None:
        kinds += [e.KIND for e in services.features.company_data]
        indexes = [IndexSpec(kind, tuple(fields)) for kind, fields in services.features.indexes]
    return StorageSchema(kinds=tuple(dict.fromkeys(kinds)), indexes=tuple(indexes))


async def startup(services: Services, *, schedules: bool = True) -> None:
    await services.repo.setup(storage_schema(services))
    ensure = getattr(services.queue, "ensure_resources", None)
    if ensure is not None:
        try:
            await ensure()
        except Exception:  # noqa: BLE001
            log.warning("could not ensure queue resources (expected outside the emulator)")
    await services.cipher.warm()
    if schedules and services.features is not None:
        for spec in services.features.schedules:
            services.scheduler.every(spec.name, spec.interval_seconds, spec.fn, jitter=spec.jitter)
        await services.scheduler.start()


async def shutdown(services: Services) -> None:
    await services.scheduler.stop()
    for closable in (services.queue, services.cache, services.repo):
        try:
            await closable.close()
        except Exception:  # noqa: BLE001
            log.warning("error closing %s", type(closable).__name__)
