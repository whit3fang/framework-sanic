"""Service container: ports + core services. Built by ``framework_sanic.bootstrap``."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .config import Settings
from .ports.cache import Cache
from .ports.connector import Connector
from .ports.platform import ParentPlatform
from .ports.queue import Queue
from .ports.repository import Repository
from .ports.runtime import ChangeLogSource, Clock, Scheduler
from .ports.security import IdTokenVerifier, KeyProvider, TokenStore, WebhookVerifier
from .services.company_service import CompanyService
from .services.contact_service import ContactService
from .services.credential_service import DialpadCredentialService, ExternalCredentialService, PlatformGateway
from .services.crypto import EnvelopeCipher
from .services.entity_map_service import EntityMapService
from .services.rate_limit import RateLimiter
from .services.session_service import OAuthStateStore, SessionService
from .services.subscription_reconciler import ReconcilerConfig, SubscriptionReconciler
from .services.target_hierarchy import TargetService

if TYPE_CHECKING:
    from .features.context import FeatureRegistry


@dataclass
class Services:
    settings: Settings
    clock: Clock
    repo: Repository
    queue: Queue
    cache: Cache
    platform: ParentPlatform
    connector: Connector
    key_provider: KeyProvider
    token_store: TokenStore
    id_tokens: IdTokenVerifier
    webhooks: WebhookVerifier
    changelog: ChangeLogSource
    scheduler: Scheduler
    cipher: EnvelopeCipher
    limiter: RateLimiter
    dialpad_credentials: DialpadCredentialService
    gateway: PlatformGateway
    external_credentials: ExternalCredentialService
    sessions: SessionService
    oauth_states: OAuthStateStore
    targets: TargetService
    contacts: ContactService
    entity_maps: EntityMapService
    companies: CompanyService
    reconciler: SubscriptionReconciler
    features: FeatureRegistry | None = field(default=None)

    @classmethod
    def build(
        cls,
        *,
        settings: Settings,
        clock: Clock,
        repo: Repository,
        queue: Queue,
        cache: Cache,
        platform: ParentPlatform,
        connector: Connector,
        key_provider: KeyProvider,
        token_store: TokenStore,
        id_tokens: IdTokenVerifier,
        webhooks: WebhookVerifier,
        changelog: ChangeLogSource,
        scheduler: Scheduler,
        cipher: EnvelopeCipher,
    ) -> Services:
        limiter = RateLimiter(cache)
        dialpad_credentials = DialpadCredentialService(token_store, platform, cache, clock)
        gateway = PlatformGateway(
            platform, dialpad_credentials, limiter, per_minute=settings.DIALPAD_RATE_LIMIT_PER_MINUTE
        )
        external_credentials = ExternalCredentialService(
            token_store, connector.auth, connector.name, cache, clock
        )
        targets = TargetService(repo, cache)
        entity_maps = EntityMapService(repo, clock)
        base = settings.PUBLIC_BASE_URL.rstrip("/")
        reconciler = SubscriptionReconciler(
            repo,
            gateway,
            targets,
            webhooks,
            cache,
            clock,
            ReconcilerConfig(
                loggable_target_types=tuple(settings.loggable_target_types),
                call_states=tuple(settings.call_states),
                subscription_kinds=(),  # set from the registered event handlers at startup
                hook_url=lambda company_id: f"{base}/webhooks/dialpad/{company_id}/events",
            ),
        )
        return cls(
            settings=settings,
            clock=clock,
            repo=repo,
            queue=queue,
            cache=cache,
            platform=platform,
            connector=connector,
            key_provider=key_provider,
            token_store=token_store,
            id_tokens=id_tokens,
            webhooks=webhooks,
            changelog=changelog,
            scheduler=scheduler,
            cipher=cipher,
            limiter=limiter,
            dialpad_credentials=dialpad_credentials,
            gateway=gateway,
            external_credentials=external_credentials,
            sessions=SessionService(
                settings.SESSION_SECRET.get_secret_value(),
                settings.SESSION_TTL_SECONDS,
                cache,
                clock,
                max_age_seconds=settings.SESSION_MAX_AGE_SECONDS,
            ),
            oauth_states=OAuthStateStore(cache),
            targets=targets,
            entity_maps=entity_maps,
            contacts=ContactService(
                entity_maps,
                connector,
                external_credentials,
                cache,
                clock,
                cache_seconds=settings.CONTACT_CACHE_SECONDS,
            ),
            companies=CompanyService(repo, queue, clock, dialpad_credentials, external_credentials),
            reconciler=reconciler,
        )
