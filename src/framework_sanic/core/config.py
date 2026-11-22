"""Configuration (env / .env locally, terraform in beta and production).

List-valued settings are comma-separated strings in the environment and exposed as parsed
properties, so they behave the same in env vars, .env files and terraform.
"""

from __future__ import annotations

from functools import cached_property
from typing import Any, Literal

from pydantic import Field, PrivateAttr, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


def _csv(raw: str) -> list[str]:
    return [p.strip() for p in raw.split(",") if p.strip()]


class Settings(BaseSettings):
    """Framework settings. Connectors and features own theirs (``PluginSettings``)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True)
    _env_file_used: Any = PrivateAttr(default=".env")
    _explicit: dict[str, Any] = PrivateAttr(default_factory=dict)

    def __init__(self, **values: Any) -> None:
        super().__init__(**values)
        # Remembered so plugin settings load from the same .env and see explicit overrides.
        self._env_file_used = values.get("_env_file", self.model_config.get("env_file"))
        self._explicit = {k: v for k, v in values.items() if not k.startswith("_")}

    def insecure_defaults(self) -> list[str]:
        """Secrets still at their public dev default. Allowed only with the fake Dialpad."""
        if self.PLATFORM_ADAPTER == "fake":
            return []
        names = ["DIALPAD_CLIENT_SECRET", "WEBHOOK_SECRET_KEY", "SESSION_SECRET"]
        if not self.KMS_KEY:  # the local key provider uses LOCAL_MASTER_KEY
            names.append("LOCAL_MASTER_KEY")
        fields = type(self).model_fields
        return [n for n in names if getattr(self, n) == fields[n].default]

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
    # Names registered under the framework_sanic.connectors / framework_sanic.features entry points.
    CONNECTOR: str = "fake"
    FEATURES: str = ""
    CONNECTOR_RATE_LIMIT_PER_MINUTE: int = 600

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
    # A session can be renewed with itself only this long after the Dialpad id_token login; then
    # the iframe must present a new id_token, so removed users and lost admin rights take effect.
    SESSION_MAX_AGE_SECONDS: int = 8 * 3600
    EMBEDDED_WORKERS: bool = True
    WORKER_CONCURRENCY: int = 32
    PER_COMPANY_CONCURRENCY: int = 4
    UI_RATE_LIMIT_PER_MINUTE: int = 600
    CONTACT_CACHE_SECONDS: int = 30
    SWEEP_TICK_SECONDS: int = 600
    LOG_LEVEL: str = "INFO"
    # Directory with the built UI (index.html + assets/) served at /app/. Default: the build
    # shipped inside the package (framework_sanic/ui_dist), when present.
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


class PluginSettings(BaseSettings):
    """Base for settings a connector or feature owns. Subclasses set an ``env_prefix``::

        class HubSpotSettings(PluginSettings):
            model_config = SettingsConfigDict(env_prefix="HUBSPOT_")
            client_id: str = ""

    and load them with ``load_settings(HubSpotSettings, settings)`` (or ``ctx.settings(...)``).
    """

    model_config = SettingsConfigDict(extra="ignore", frozen=True)


def load_settings[P: BaseSettings](cls: type[P], base: Settings) -> P:
    """Load plugin settings the way ``base`` was loaded: environment variables, then the same .env
    file (none in tests). Prefixed keys passed explicitly to ``Settings(...)`` win, so tests can
    write ``make_settings(HUBSPOT_PORTAL_ID="42")``."""
    prefix = str(cls.model_config.get("env_prefix") or "").upper()
    fields = {name.lower(): name for name in cls.model_fields}
    overrides: dict[str, Any] = {}
    for key, value in base._explicit.items():
        upper = key.upper()
        if prefix and upper.startswith(prefix) and upper[len(prefix) :].lower() in fields:
            overrides[fields[upper[len(prefix) :].lower()]] = value
    return cls(_env_file=base._env_file_used, **overrides)
