"""framework_sanic.sdk: the public API for integrations (connectors and features).

Integration code imports from here (and ``framework_sanic.testing`` in tests) and nothing else
from the framework: ``framework-sanic check``
and ``tests/unit/test_sdk.py`` enforce it. Everything outside the SDK is internal and may change
without notice; names here follow semantic versioning once the framework is published as a
package, with a deprecation warning for at least one minor release before removal.

Plugins are found through entry points in the integration's ``pyproject.toml``::

    [project.entry-points."framework_sanic.connectors"]
    acme = "acme_integration.connector:make"          # make(settings) -> Connector

    [project.entry-points."framework_sanic.features"]
    "acme.calls" = "acme_integration.listeners.calls:feature"   # a CallListener
    "acme.tickets" = "acme_integration.features.tickets:feature"

Dialpad activity reaches the integration through listeners (``CallListener``, ``SmsListener``,
``NoteListener``); what happens to it is entirely the integration's.
"""

from __future__ import annotations

from framework_sanic.connectors.rest import OAuth2Auth, OAuth2Config, RestConnector, parse_retry_after
from framework_sanic.core.config import PluginSettings, Settings, load_settings
from framework_sanic.core.container import Services
from framework_sanic.core.domain.entities import Entity as StoredEntity
from framework_sanic.core.domain.entities import EntityMap, OutboxEntry
from framework_sanic.core.domain.errors import (
    ConfigurationError,
    Conflict,
    Forbidden,
    InvalidInput,
    NotConnected,
    NotFound,
)
from framework_sanic.core.domain.events import CallEvent, SmsEvent
from framework_sanic.core.domain.records import Lane, Record, stable_hash
from framework_sanic.core.features.context import Feature, FeatureContext
from framework_sanic.core.features.guards import SyncGuards
from framework_sanic.core.features.jobs import ACK, JobResult, backoff, deadletter, retry
from framework_sanic.core.intake import EventKind, InboundEvent, decrypt_sms_text
from framework_sanic.core.ports.connector import (
    Caller,
    Connector,
    ConnectorAuth,
    ConnectorCredential,
    ConnectorError,
    ContactMatch,
    Entity,
    EntityQuery,
    ManifestField,
    NewEntity,
    UiManifest,
)
from framework_sanic.core.ports.platform import OAuthTokens
from framework_sanic.core.ports.queue import TOPIC_CONTROL, TOPIC_EVENTS, TOPIC_SYNC, QueueMessage
from framework_sanic.core.ports.repository import UnitOfWork, run_in_uow
from framework_sanic.core.services.matching import match_by_phone
from framework_sanic.core.services.outbox import publish_and_clear
from framework_sanic.core.services.phone import phone_variants, to_e164
from framework_sanic.core.services.session_service import Principal
from framework_sanic.http.routes import RequestContext, route
from framework_sanic.listeners import (
    CallActivity,
    CallListener,
    NoteIn,
    NoteListener,
    NoteOut,
    SmsActivity,
    SmsListener,
)

__all__ = [
    # listeners: where Dialpad activity arrives
    "CallActivity",
    "CallListener",
    "NoteIn",
    "NoteListener",
    "NoteOut",
    "SmsActivity",
    "SmsListener",
    # features
    "ACK",
    "TOPIC_CONTROL",
    "TOPIC_EVENTS",
    "TOPIC_SYNC",
    "CallEvent",
    "EventKind",
    "Feature",
    "FeatureContext",
    "InboundEvent",
    "JobResult",
    "Lane",
    "OutboxEntry",
    "QueueMessage",
    "Record",
    "Services",
    "SmsEvent",
    "StoredEntity",
    "SyncGuards",
    "UnitOfWork",
    "backoff",
    "deadletter",
    "decrypt_sms_text",
    "publish_and_clear",
    "retry",
    "run_in_uow",
    "stable_hash",
    # HTTP routes contributed by features
    "Principal",
    "RequestContext",
    "route",
    # connectors
    "Caller",
    "Connector",
    "ConnectorAuth",
    "ConnectorCredential",
    "ConnectorError",
    "ContactMatch",
    "Entity",
    "EntityQuery",
    "EntityMap",
    "ManifestField",
    "NewEntity",
    "OAuth2Auth",
    "OAuth2Config",
    "OAuthTokens",
    "RestConnector",
    "UiManifest",
    "match_by_phone",
    "parse_retry_after",
    "phone_variants",
    "to_e164",
    # settings and errors
    "ConfigurationError",
    "Conflict",
    "Forbidden",
    "InvalidInput",
    "NotConnected",
    "NotFound",
    "PluginSettings",
    "Settings",
    "load_settings",
]
