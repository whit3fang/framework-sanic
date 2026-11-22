"""HubSpot settings (``HUBSPOT_*`` environment variables / .env)."""

from __future__ import annotations

from pydantic import SecretStr
from pydantic_settings import SettingsConfigDict

from framework_sanic.sdk import PluginSettings


class HubSpotSettings(PluginSettings):
    model_config = SettingsConfigDict(env_prefix="HUBSPOT_")

    client_id: str = ""
    client_secret: SecretStr = SecretStr("")
    api_base: str = "https://api.hubapi.com"
    portal_id: str = ""  # fallback for record links when the OAuth token carries none
