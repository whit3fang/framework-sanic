"""HubSpot OAuth: the framework's generic ``OAuth2Auth`` with HubSpot's endpoints and scopes, plus
an ``identify`` step that stores the portal (hub) id and HubSpot user id with the credential.

VERIFY: minimal scope set for calls/communications/notes, and PKCE support on the authorize
endpoint (the challenge is sent; HubSpot ignores it if unsupported).
"""

from __future__ import annotations

from typing import Any

import httpx

from framework_sanic.sdk import OAuth2Auth, OAuth2Config, OAuthTokens

AUTHORIZE_URL = "https://app.hubspot.com/oauth/authorize"
SCOPES = ["oauth", "crm.objects.contacts.read", "crm.objects.contacts.write", "crm.schemas.calls.write"]


def hubspot_auth(*, client_id: str, client_secret: str, api_base: str, http: httpx.AsyncClient) -> OAuth2Auth:
    async def identify(tokens: OAuthTokens) -> dict[str, Any]:
        info = await http.get(f"{api_base}/oauth/v1/access-tokens/{tokens.access_token}")
        if info.status_code != 200:
            return {}
        meta = info.json()
        return {"external_user_id": meta.get("user_id") or "", "portal_id": meta.get("hub_id") or ""}

    config = OAuth2Config(
        authorize_url=AUTHORIZE_URL,
        token_url=f"{api_base}/oauth/v1/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )
    return OAuth2Auth(config, http, identify=identify)
