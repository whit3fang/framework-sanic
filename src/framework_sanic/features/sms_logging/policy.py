"""Which SMS ``sms_logging`` logs and what it sends: settings (``SMS_LOGGING_*``) plus an optional
connector hook (``SmsLogFilter.should_log_sms``)."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic_settings import SettingsConfigDict

from framework_sanic.core.config import PluginSettings

from .models import SmsActivity


class SmsLoggingSettings(PluginSettings):
    model_config = SettingsConfigDict(env_prefix="SMS_LOGGING_")

    directions: str = "inbound,outbound"
    include_text: bool = True  # False: log the SMS without its text (never even decrypted)

    @property
    def direction_set(self) -> set[str]:
        return {p.strip() for p in self.directions.split(",") if p.strip()}


@runtime_checkable
class SmsLogFilter(Protocol):
    """Optional connector hook: return False to not log this SMS."""

    def should_log_sms(self, activity: SmsActivity) -> bool: ...


class SmsLogPolicy:
    def __init__(self, settings: SmsLoggingSettings, connector: Any) -> None:
        self.settings = settings
        self.directions = settings.direction_set
        self.hook = connector if isinstance(connector, SmsLogFilter) else None

    def direction_allowed(self, direction: str | None) -> bool:
        return not direction or direction in self.directions

    def should_log(self, act: SmsActivity) -> bool:
        return self.hook.should_log_sms(act) if self.hook else True
