"""HubSpot integration for the Dialpad integration framework (entry point ``hubspot``).

Package layout (each module has one job):

  connector.py   HubSpotConnector: the framework's ports, delegating to the modules below
  contacts.py    caller -> contact matching, contact search / get / create (the contact card)
  calls.py       call logging: calls, recording links, notes
  messages.py    SMS logging
  references.py  idempotency: the dialpad_activity_key property and lookups by it
  mapping.py     pure Dialpad -> HubSpot property mapping
  crm.py         the HubSpot CRM API (HTTP only)
  auth.py        OAuth (endpoints, scopes, portal identification)
  manifest.py    what the contact card shows
  settings.py    HUBSPOT_* settings
  features/      HubSpot-only features (sms_campaign placeholder)
"""

from __future__ import annotations

from framework_sanic.sdk import Settings, load_settings

from .connector import HubSpotConnector
from .mapping import REFERENCE_PROPERTY
from .settings import HubSpotSettings


def make(settings: Settings) -> HubSpotConnector:
    """``framework_sanic.connectors`` entry point: build the connector from its own settings."""
    hs = load_settings(HubSpotSettings, settings)
    return HubSpotConnector(
        client_id=hs.client_id,
        client_secret=hs.client_secret.get_secret_value(),
        api_base=hs.api_base,
        portal_id=hs.portal_id,
    )


__all__ = ["REFERENCE_PROPERTY", "HubSpotConnector", "HubSpotSettings", "make"]
