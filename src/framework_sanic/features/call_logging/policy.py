"""Which calls ``call_logging`` logs: settings (``CALL_LOGGING_*``) plus an optional connector hook.

Most integrations only need settings. For rules settings cannot express, the connector implements
``CallLogFilter.should_log_call``. Decisions are made when a call is about to be synced (it is
terminal); once a call has been logged, later changes (recordings, notes) always sync.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic_settings import SettingsConfigDict

from framework_sanic.core.config import PluginSettings

from .models import CallActivity


def _csv(raw: str) -> set[str]:
    return {p.strip() for p in raw.split(",") if p.strip()}


class CallLoggingSettings(PluginSettings):
    model_config = SettingsConfigDict(env_prefix="CALL_LOGGING_")

    directions: str = "inbound,outbound"
    skip_states: str = ""  # e.g. "missed,voicemail"
    min_duration_seconds: float = 0.0  # completed (hangup) calls shorter than this are not logged
    recordings: bool = True  # attach recording links when the connector supports them

    @property
    def direction_set(self) -> set[str]:
        return _csv(self.directions)

    @property
    def skip_state_set(self) -> set[str]:
        return _csv(self.skip_states)


@runtime_checkable
class CallLogFilter(Protocol):
    """Optional connector hook: return False to not log this call."""

    def should_log_call(self, activity: CallActivity) -> bool: ...


class CallLogPolicy:
    def __init__(self, settings: CallLoggingSettings, connector: Any) -> None:
        self.settings = settings
        self.directions = settings.direction_set
        self.skip_states = settings.skip_state_set
        self.hook = connector if isinstance(connector, CallLogFilter) else None

    def should_log(self, act: CallActivity) -> bool:
        if act.direction and act.direction not in self.directions:
            return False
        if act.state in self.skip_states:
            return False
        min_ms = self.settings.min_duration_seconds * 1000
        if min_ms and act.state == "hangup" and (act.duration_ms or 0) < min_ms:
            return False
        return self.hook.should_log_call(act) if self.hook else True
