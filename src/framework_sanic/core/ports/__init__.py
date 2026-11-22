"""Ports: every external dependency sits behind one of these interfaces."""

from .cache import Cache, CacheUnavailable
from .connector import Connector, ConnectorAuth, ConnectorCredential, ConnectorError, UiManifest
from .platform import ParentPlatform
from .queue import Delivery, Queue, QueueMessage
from .repository import Repository, UnitOfWork, run_in_uow
from .runtime import ChangeLogSource, Clock, Scheduler, SystemClock
from .security import IdTokenVerifier, KeyProvider, TokenStore, WebhookVerifier

__all__ = [
    "Cache",
    "CacheUnavailable",
    "ChangeLogSource",
    "Clock",
    "Connector",
    "ConnectorAuth",
    "ConnectorCredential",
    "ConnectorError",
    "Delivery",
    "IdTokenVerifier",
    "KeyProvider",
    "ParentPlatform",
    "Queue",
    "QueueMessage",
    "Repository",
    "Scheduler",
    "SystemClock",
    "TokenStore",
    "UiManifest",
    "UnitOfWork",
    "WebhookVerifier",
    "run_in_uow",
]
