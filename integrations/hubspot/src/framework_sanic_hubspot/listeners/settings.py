"""What HubSpot logging logs (environment variables / .env)."""

from __future__ import annotations

from typing import Literal

from pydantic_settings import SettingsConfigDict

from framework_sanic.sdk import PluginSettings

UnknownCaller = Literal["log_unassociated", "create_contact", "skip"]


def csv(raw: str) -> set[str]:
    return {p.strip() for p in raw.split(",") if p.strip()}


class CallLogSettings(PluginSettings):
    model_config = SettingsConfigDict(env_prefix="HUBSPOT_CALLS_")

    directions: str = "inbound,outbound"
    skip_states: str = ""  # e.g. "missed,voicemail"
    min_duration_seconds: float = 0.0  # completed (hangup) calls shorter than this are not logged
    recordings: bool = True  # attach recording links
    unknown_caller: UnknownCaller = "log_unassociated"  # VERIFY: default for unknown callers
    retention_days: int = 90  # how long call records are kept locally

    def should_log(self, direction: str | None, state: str | None, duration_ms: float | None) -> bool:
        if direction and direction not in csv(self.directions):
            return False
        if state in csv(self.skip_states):
            return False
        min_ms = self.min_duration_seconds * 1000
        return not (min_ms and state == "hangup" and (duration_ms or 0) < min_ms)


class SmsLogSettings(PluginSettings):
    model_config = SettingsConfigDict(env_prefix="HUBSPOT_SMS_")

    directions: str = "inbound,outbound"
    include_text: bool = True  # False: log the SMS without its text (never even decrypted)
    unknown_caller: UnknownCaller = "log_unassociated"
    retention_days: int = 90

    def direction_allowed(self, direction: str | None) -> bool:
        return not direction or direction in csv(self.directions)
