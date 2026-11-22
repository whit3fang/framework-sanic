"""Composition root: turns Settings into adapters, services, features, workers and schedules.

Adapters are swappable by config (STORAGE_ADAPTER, QUEUE_ADAPTER, CACHE_URL, KMS_KEY,
PLATFORM_ADAPTER). Connectors and features are plugins found through entry points
(``framework_sanic.connectors`` / ``framework_sanic.features``), so an integration package is
picked up by being installed. Tests pass explicit adapter instances through ``overrides``.
"""

from __future__ import annotations

import dataclasses
import importlib
import importlib.util
import logging
from importlib.metadata import entry_points
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
# Top-level modules each extra provides (see [project.optional-dependencies]).
EXTRA_MODULES: dict[str, tuple[str, ...]] = {
    "gcp": ("google.cloud.datastore", "google.cloud.pubsub_v1", "google.cloud.kms"),
    "redis": ("redis",),
    "sql": ("sqlalchemy", "aiosqlite"),
}


def _installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except ModuleNotFoundError:  # a parent package (e.g. google.cloud) is missing
        return False


def require_extra(extra: str, setting: str, *modules: str) -> None:
    """Fail with an actionable message when the client library behind ``setting`` is missing."""
    missing = [m for m in modules or EXTRA_MODULES[extra] if not _installed(m)]
    if missing:
        raise ConfigurationError(
            f"{setting} needs the '{extra}' extra (missing: {', '.join(missing)}): "
            f"pip install 'framework-sanic[{extra}]' (in this repository: uv sync --all-extras)"
        )


CONNECTOR_ENTRY_POINTS = "framework_sanic.connectors"


def load_factory(setting: str, path: str) -> Any:
    """Load ``package.module:attr``; configuration errors name the setting."""
    module_name, _, attr = path.partition(":")
    try:
        return getattr(importlib.import_module(module_name), attr)
    except (ImportError, AttributeError) as exc:
        raise ConfigurationError(f"{setting} {path!r} cannot be loaded: {exc}") from exc


def make_repository(settings: Settings, clock: Any) -> Repository:
    """Built-in adapters by name, or ``package.module:factory`` -> ``factory(settings, clock)``."""
    name = settings.STORAGE_ADAPTER
    if ":" in name:
        repo = load_factory("STORAGE_ADAPTER", name)(settings, clock)
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
        require_extra("sql", f"STORAGE_ADAPTER={name}", "sqlalchemy", "aiosqlite")
        from framework_sanic.adapters.storage.sqlalchemy.repository import SqlAlchemyRepository

        return SqlAlchemyRepository(settings.DATABASE_URL, clock=clock)
    require_extra("gcp", f"STORAGE_ADAPTER={name}", "google.cloud.datastore")
    from framework_sanic.adapters.storage.datastore.repository import DatastoreRepository

    return DatastoreRepository(
        settings.DATASTORE_PROJECT, namespace=settings.DATASTORE_NAMESPACE, clock=clock
    )


def make_queue(settings: Settings) -> Any:
    if settings.QUEUE_ADAPTER == "memory":
        from framework_sanic.adapters.queue.memory import MemoryQueue

        return MemoryQueue(settings.SHARD_COUNT)
    require_extra("gcp", f"QUEUE_ADAPTER={settings.QUEUE_ADAPTER}", "google.cloud.pubsub_v1")
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
    require_extra("redis", "CACHE_URL=redis://...", "redis")
    from framework_sanic.adapters.cache.redis import RedisCache

    return RedisCache(settings.CACHE_URL)


def make_key_provider(settings: Settings) -> Any:
    if settings.KMS_KEY:
        require_extra("gcp", "KMS_KEY", "google.cloud.kms")
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


def installed_connectors() -> list[str]:
    return sorted(ep.name for ep in entry_points(group=CONNECTOR_ENTRY_POINTS))


def make_connector(settings: Settings) -> Connector:
    """``CONNECTOR`` names an installed connector (entry point) or a ``package.module:factory``.
    The factory is called as ``factory(settings)`` and loads its own settings."""
    name = settings.CONNECTOR
    eps = {ep.name: ep for ep in entry_points(group=CONNECTOR_ENTRY_POINTS)}
    if name in eps:
        try:
            factory = eps[name].load()
        except (ImportError, AttributeError) as exc:
            raise ConfigurationError(f"CONNECTOR {name!r} cannot be loaded: {exc}") from exc
    elif ":" in name:
        factory = load_factory("CONNECTOR", name)
    else:
        raise ConfigurationError(f"unknown CONNECTOR {name!r}; installed: {installed_connectors()}")
    try:
        connector = factory(settings)
    except TypeError as exc:  # e.g. a class or a function with another signature
        raise ConfigurationError(f"CONNECTOR {name!r} must be callable as factory(settings): {exc}") from exc
    if not isinstance(connector, Connector):
        raise ConfigurationError(
            f"CONNECTOR {name!r} returned {type(connector).__name__}, which does not implement the "
            "Connector port (framework_sanic.sdk.Connector)"
        )
    return connector


def build_services(settings: Settings | None = None, **overrides: Any) -> Services:
    settings = settings or Settings()
    if insecure := settings.insecure_defaults():
        raise ConfigurationError(
            f"{', '.join(insecure)} still use the public dev defaults; set real secrets "
            "(only PLATFORM_ADAPTER=fake may run with them)"
        )
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
