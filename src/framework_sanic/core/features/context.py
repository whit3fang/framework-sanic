"""Feature extension model.

A feature registers everything through ``FeatureContext``: event handlers, routes, job handlers,
schedules, declared scopes, declared config keys, storage indexes and the entity kinds it owns.

The framework receives events and delivers them; what happens next is the feature's business.
``ctx.on_event("call", handler)`` is the only way to receive Dialpad events, and Dialpad webhook
subscriptions are created only for the event kinds some enabled feature handles.
Features share credentials and the Repository via ``ctx.services`` and cannot import each other
(enforced by ``tests/unit/test_import_rules.py``).
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from types import ModuleType
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar

from pydantic_settings import BaseSettings

from ..config import load_settings
from ..domain.entities import Entity
from ..domain.errors import ConfigurationError
from ..intake.delivery import EVENT_KINDS, EventHandler, EventKind, event_job
from ..ports.queue import TOPIC_EVENTS
from ..services.crypto import EnvelopeCipher
from .jobs import JobHandler

if TYPE_CHECKING:
    from ..container import Services

log = logging.getLogger(__name__)

S = TypeVar("S", bound=BaseSettings)

# Features are discovered by name from this entry-point group: the framework's own (in its
# pyproject.toml) and every installed integration's. A dotted module path also works (tests).
FEATURE_ENTRY_POINTS = "framework_sanic.features"
BUILTIN_FEATURES: dict[str, str] = {
    "platform": "framework_sanic.core.features.platform",
}
# Always on: subscription lifecycle, outbox relay. Owns no event handling.
PLATFORM_FEATURES: tuple[str, ...] = ("platform",)


class Feature(Protocol):
    name: str

    def setup(self, ctx: FeatureContext) -> None: ...


@dataclass
class ScheduleSpec:
    name: str
    interval_seconds: float
    fn: Callable[[], Awaitable[None]]
    jitter: float = 0.0


@dataclass
class FeatureContext:
    name: str
    services: Services
    routes: list[Callable[..., Any]] = field(default_factory=list)
    jobs: dict[tuple[str, str], JobHandler] = field(default_factory=dict)
    schedules: list[ScheduleSpec] = field(default_factory=list)
    event_handlers: dict[EventKind, str] = field(default_factory=dict)  # kind -> job name
    scopes: set[str] = field(default_factory=set)
    config_keys: set[str] = field(default_factory=set)
    indexes: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    company_data: list[type[Entity]] = field(default_factory=list)

    def on_event(self, kind: EventKind, handler: EventHandler) -> EventHandler:
        """Receive every Dialpad ``kind`` event for logging-enabled targets (at least once).

        Each feature gets its own queue message per event, so its retries are independent of
        other features. Registering a handler also subscribes to the kind on Dialpad.
        """
        if kind not in EVENT_KINDS:
            raise ConfigurationError(f"unknown event kind {kind!r}; expected one of {EVENT_KINDS}")
        if kind in self.event_handlers:
            raise ConfigurationError(f"feature {self.name} registered two {kind} handlers")
        job_name = f"event.{kind}:{self.name}"
        self.event_handlers[kind] = job_name
        self.jobs[(TOPIC_EVENTS, job_name)] = event_job(kind, handler, self._cipher)
        return handler

    def _cipher(self) -> EnvelopeCipher | None:
        services: Services | None = self.services
        return services.cipher if services is not None else None

    def route(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        """Register a handler decorated with ``@route``."""
        self.routes.append(fn)
        return fn

    def job(self, topic: str, name: str) -> Callable[[JobHandler], JobHandler]:
        def deco(fn: JobHandler) -> JobHandler:
            self.jobs[(topic, name)] = fn
            return fn

        return deco

    def schedule(
        self, name: str, interval_seconds: float, fn: Callable[[], Awaitable[None]], *, jitter: float = 0.0
    ) -> None:
        self.schedules.append(ScheduleSpec(f"{self.name}.{name}", interval_seconds, fn, jitter))

    def require_scopes(self, *scopes: str) -> None:
        self.scopes.update(scopes)

    def declare_config(self, *keys: str) -> None:
        self.config_keys.update(keys)

    def declare_index(self, kind: str, *fields: str) -> None:
        self.indexes.append((kind, fields))

    def declare_company_data(self, *entities: type[Entity]) -> None:
        """Entity kinds holding company data: deleted on uninstall."""
        self.company_data.extend(entities)

    def settings(self, cls: type[S]) -> S:
        """This feature's own settings (a ``PluginSettings`` subclass with an env prefix)."""
        return load_settings(cls, self.services.settings)


class FeatureRegistry:
    def __init__(self) -> None:
        self.contexts: list[FeatureContext] = []

    @property
    def routes(self) -> list[Callable[..., Any]]:
        return [r for c in self.contexts for r in c.routes]

    @property
    def jobs(self) -> dict[tuple[str, str], JobHandler]:
        out: dict[tuple[str, str], JobHandler] = {}
        for c in self.contexts:
            for key, handler in c.jobs.items():
                if key in out:
                    raise ConfigurationError(f"job {key} registered twice")
                out[key] = handler
        return out

    @property
    def schedules(self) -> list[ScheduleSpec]:
        return [s for c in self.contexts for s in c.schedules]

    @property
    def event_routes(self) -> dict[EventKind, tuple[str, ...]]:
        """Event kind -> job names of every handler, in feature load order (used by accept)."""
        out: dict[EventKind, list[str]] = {}
        for c in self.contexts:
            for kind, job in c.event_handlers.items():
                out.setdefault(kind, []).append(job)
        return {k: tuple(v) for k, v in out.items()}

    @property
    def subscription_kinds(self) -> tuple[Literal["call", "sms"], ...]:
        """Dialpad subscription kinds follow the registered handlers."""
        return tuple(sorted(self.event_routes))

    @property
    def company_data(self) -> tuple[type[Entity], ...]:
        return tuple(e for c in self.contexts for e in c.company_data)

    @property
    def scopes(self) -> set[str]:
        return {s for c in self.contexts for s in c.scopes}

    @property
    def indexes(self) -> list[tuple[str, tuple[str, ...]]]:
        return [i for c in self.contexts for i in c.indexes]


def installed_features() -> list[str]:
    return sorted(ep.name for ep in entry_points(group=FEATURE_ENTRY_POINTS))


def load_feature(name: str) -> Feature:
    """Resolve a ``FEATURES`` entry: built-in, then entry point, then dotted module path."""
    target: Any
    if name in BUILTIN_FEATURES:
        target = importlib.import_module(BUILTIN_FEATURES[name])
    else:
        eps = {ep.name: ep for ep in entry_points(group=FEATURE_ENTRY_POINTS)}
        if name in eps:
            target = eps[name].load()
        elif "." in name:
            try:
                target = importlib.import_module(name)
            except ModuleNotFoundError as exc:
                raise ConfigurationError(
                    f"unknown feature {name!r}: not an installed feature ({installed_features()}) "
                    f"or an importable module ({exc})"
                ) from exc
        else:
            raise ConfigurationError(f"unknown feature {name!r}; installed: {installed_features()}")
    feature = getattr(target, "feature", None) if isinstance(target, ModuleType) else target
    if feature is None or not callable(getattr(feature, "setup", None)):
        raise ConfigurationError(f"feature {name!r} does not resolve to an object with setup(ctx)")
    return feature  # type: ignore[no-any-return]


def load_features(services: Services, names: list[str]) -> FeatureRegistry:
    registry = FeatureRegistry()
    ordered = list(PLATFORM_FEATURES) + [n for n in names if n not in PLATFORM_FEATURES]
    for name in ordered:
        feature = load_feature(name)
        ctx = FeatureContext(name=feature.name, services=services)
        feature.setup(ctx)
        registry.contexts.append(ctx)
    validate_scopes(registry, services.settings.dialpad_scopes)
    if not registry.event_routes:
        log.warning(
            "no enabled feature handles Dialpad events (FEATURES=%r): nothing will be subscribed "
            "or processed. Enable e.g. FEATURES=call_logging.",
            services.settings.FEATURES,
        )
    return registry


def validate_scopes(registry: FeatureRegistry, granted: list[str]) -> None:
    """Refuse to start if an enabled feature needs a scope that is not in DIALPAD_SCOPES."""
    granted_set = set(granted)
    missing = sorted(s for s in registry.scopes if s not in granted_set)
    if missing:
        raise ConfigurationError(f"DIALPAD_SCOPES is missing scopes required by enabled features: {missing}")
