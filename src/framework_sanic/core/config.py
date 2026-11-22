"""Configuration (env / .env locally, terraform in beta and production).

List-valued settings are comma-separated strings in the environment and exposed as parsed
properties, so they behave the same in env vars, .env files and terraform.
"""

from __future__ import annotations

from functools import cached_property
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


def _csv(raw: str) -> list[str]:
    return [p.strip() for p in raw.split(",") if p.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True)

    # --- Dialpad ---------------------------------------------------------------------------
    DIALPAD_CLIENT_ID: str = "dev-client-id"
    DIALPAD_CLIENT_SECRET: SecretStr = SecretStr("dev-client-secret-change-me-0123456789")
    DIALPAD_SCOPES: str = "recordings_export,message_content_export,calls:list,change_log"
    DIALPAD_IFRAME_URL: str = "http://localhost:8000/app/"
    DIALPAD_API_BASE: str = "https://dialpad.com"
    DIALPAD_FRAME_ORIGINS: str = "https://dialpad.com,https://*.dialpad.com"
    # VERIFY: default call-state list.
    DIALPAD_CALL_STATES: str = "hangup,missed,voicemail,recording"
    LOGGABLE_TARGET_TYPES: str = "user,department,callcenter"
    DIALPAD_RATE_LIMIT_PER_MINUTE: int = 1200  # VERIFY: burst vs sustained enforcement
    PLATFORM_ADAPTER: Literal["dialpad", "fake"] = "dialpad"
    CHANGELOG_LIFECYCLE_ACTIONS: str = "target_created,target_deleted"

    # --- Connector and features ------------------------------------------------------------
    CONNECTOR: str = "fake"
    FEATURES: str = ""
    CONNECTOR_RATE_LIMIT_PER_MINUTE: int = 600
    # VERIFY: default behaviour for unknown callers.
    UNKNOWN_CALLER_POLICY: Literal["log_unassociated", "create_contact", "skip"] = "log_unassociated"
    HUBSPOT_CLIENT_ID: str = ""
    HUBSPOT_CLIENT_SECRET: SecretStr = SecretStr("")
    HUBSPOT_API_BASE: str = "https://api.hubapi.com"
    HUBSPOT_PORTAL_ID: str = ""

    # --- Infrastructure --------------------------------------------------------------------
    # "memory" | "sqlalchemy" | "datastore", or "package.module:factory" for your own adapter:
    # ``factory(settings, clock) -> Repository`` (see README "Writing a storage adapter").
    STORAGE_ADAPTER: str = "memory"
    DATABASE_URL: str = "sqlite+aiosqlite:///./framework.db"
    DATASTORE_PROJECT: str = "local-project"
    DATASTORE_NAMESPACE: str | None = None
    QUEUE_ADAPTER: Literal["memory", "pubsub"] = "memory"
    PUBSUB_PROJECT: str = "local-project"
    PUBSUB_PREFIX: str = "integration"
    CACHE_URL: str = "memory://"
    KMS_KEY: str = ""  # projects/.../cryptoKeys/...; empty = local key provider
    LOCAL_MASTER_KEY: SecretStr = SecretStr("ZGV2LWxvY2FsLW1hc3Rlci1rZXktMzItYnl0ZXMhIQ==")
    SHARD_COUNT: int = 8
    ACTIVITY_TTL_DAYS: int = 90
    # Expired records are hidden on read immediately; this deletes them physically.
    PURGE_INTERVAL_SECONDS: float = 3600.0
    PURGE_BATCH_SIZE: int = 1000
    PURGE_MAX_BATCHES: int = 50  # per run, so one run never monopolises the database
    HTTP_ADAPTER: Literal["sanic"] = "sanic"

    # --- Service ---------------------------------------------------------------------------
    PUBLIC_BASE_URL: str = "http://localhost:8000"
    UI_ORIGINS: str = "http://localhost:5173,http://localhost:8000"
    WEBHOOK_SECRET_KEY: SecretStr = SecretStr("dev-webhook-master-key")
    SESSION_SECRET: SecretStr = SecretStr("dev-session-secret-change-me-32bytes")
    SESSION_TTL_SECONDS: int = 900
    EMBEDDED_WORKERS: bool = True
    WORKER_CONCURRENCY: int = 32
    PER_COMPANY_CONCURRENCY: int = 4
    UI_RATE_LIMIT_PER_MINUTE: int = 600
    CONTACT_CACHE_SECONDS: int = 30
    SWEEP_TICK_SECONDS: int = 600
    LOG_LEVEL: str = "INFO"
    SERVE_UI_DIR: str | None = Field(
        default=None, description="VERIFY: static hosting vs served by the service"
    )

    @cached_property
    def dialpad_scopes(self) -> list[str]:
        return _csv(self.DIALPAD_SCOPES)

    @cached_property
    def call_states(self) -> list[str]:
        return _csv(self.DIALPAD_CALL_STATES)

    @cached_property
    def loggable_target_types(self) -> list[str]:
        return _csv(self.LOGGABLE_TARGET_TYPES)

    @cached_property
    def features(self) -> list[str]:
        return _csv(self.FEATURES)

    @cached_property
    def frame_origins(self) -> list[str]:
        return _csv(self.DIALPAD_FRAME_ORIGINS)

    @cached_property
    def ui_origins(self) -> list[str]:
        return _csv(self.UI_ORIGINS)

    @cached_property
    def changelog_lifecycle_actions(self) -> list[str]:
        return _csv(self.CHANGELOG_LIFECYCLE_ACTIONS)

    @property
    def activity_ttl_seconds(self) -> float:
        return self.ACTIVITY_TTL_DAYS * 86400.0
