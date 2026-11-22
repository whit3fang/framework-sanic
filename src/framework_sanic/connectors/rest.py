"""Toolkit for connectors that talk to an HTTP/JSON API (most CRMs and ticketing systems).

A connector built on it declares endpoints and field mappings. The plumbing every integration
used to rewrite is here, once:

* ``OAuth2Auth``: authorization-code flow with PKCE, code exchange, refresh (keeps the refresh
  token when the provider does not rotate it), optional ``identify`` hook for account metadata.
* ``RestConnector``: bearer-authenticated requests, a shared ``httpx.AsyncClient`` with timeouts,
  one quick retry of idempotent requests on network errors and 502/503/504, ``paginate`` and
  ``map_error`` (status codes and ``Retry-After`` -> ``ConnectorError`` kinds the framework's
  retry/park/deadletter logic understands).

Job-level retries (backoff, parking, deadletter) stay in the framework (``SyncGuards``); the quick
retry here only smooths over a dropped connection.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import AsyncIterator, Awaitable, Callable
from email.utils import parsedate_to_datetime
from typing import Any, Literal
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, Field

from framework_sanic.core.ports.connector import ConnectorAuth, ConnectorCredential, ConnectorError
from framework_sanic.core.ports.platform import OAuthTokens

IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})
RETRYABLE_STATUS = frozenset({502, 503, 504})
DEFAULT_RETRY_AFTER = 10.0


def parse_retry_after(value: str | None, default: float = DEFAULT_RETRY_AFTER) -> float:
    """``Retry-After`` as seconds or an HTTP date; ``default`` when absent or unparseable."""
    if not value:
        return default
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return default
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.UTC)
    return max(0.0, (when - dt.datetime.now(dt.UTC)).total_seconds())


class OAuth2Config(BaseModel):
    authorize_url: str
    token_url: str
    client_id: str
    client_secret: str
    scopes: list[str] = Field(default_factory=list)
    scope_separator: str = " "
    pkce: bool = True
    client_auth: Literal["body", "basic"] = "body"  # where the client credentials go on token calls
    extra_authorize_params: dict[str, str] = Field(default_factory=dict)


Identify = Callable[[OAuthTokens], Awaitable[dict[str, Any]]]


class OAuth2Auth:
    """Generic OAuth2 authorization-code (+ PKCE) implementation of ``ConnectorAuth``.

    ``identify(tokens)`` may return account metadata stored with the credential in
    ``tokens.extra`` (e.g. ``external_user_id``, a portal or tenant id)."""

    kind: Literal["oauth2", "api_key"] = "oauth2"

    def __init__(
        self, config: OAuth2Config, http: httpx.AsyncClient, identify: Identify | None = None
    ) -> None:
        self.config = config
        self.http = http
        self.identify = identify
        self.scopes: list[str] = config.scopes

    def authorize_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str:
        c = self.config
        q = {"client_id": c.client_id, "redirect_uri": redirect_uri, "response_type": "code", "state": state}
        if c.scopes:
            q["scope"] = c.scope_separator.join(c.scopes)
        if c.pkce:
            q.update(code_challenge=code_challenge, code_challenge_method="S256")
        q.update(c.extra_authorize_params)
        sep = "&" if "?" in c.authorize_url else "?"
        return f"{c.authorize_url}{sep}{urlencode(q)}"

    async def _token(self, data: dict[str, str]) -> OAuthTokens:
        c = self.config
        kwargs: dict[str, Any] = {}
        if c.client_auth == "basic":
            kwargs["auth"] = (c.client_id, c.client_secret)
        else:
            data = {**data, "client_id": c.client_id, "client_secret": c.client_secret}
        resp = await self.http.post(c.token_url, data=data, headers={"Accept": "application/json"}, **kwargs)
        if resp.status_code in (400, 401):  # invalid_grant / invalid_client: reconnect needed
            raise ConnectorError("auth", f"token endpoint rejected the request ({resp.status_code})")
        resp.raise_for_status()
        body = resp.json()
        return OAuthTokens(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token"),
            expires_in=body.get("expires_in"),
        )

    async def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> OAuthTokens:
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}
        if self.config.pkce:
            data["code_verifier"] = code_verifier
        tokens = await self._token(data)
        if self.identify is not None:
            tokens.extra = {k: str(v) for k, v in (await self.identify(tokens)).items() if v is not None}
        return tokens

    async def refresh(self, tokens: OAuthTokens) -> OAuthTokens:
        if not tokens.refresh_token:
            raise ConnectorError("auth", "no refresh token")
        new = await self._token({"grant_type": "refresh_token", "refresh_token": tokens.refresh_token})
        if not new.refresh_token:
            new.refresh_token = tokens.refresh_token
        new.extra = tokens.extra
        return new


class RestConnector:
    """Base class for HTTP/JSON connectors. Subclasses set ``name``, ``capabilities`` and ``auth``
    and implement the entity methods (plus feature ports such as ``CallLogger``) with
    ``self.request(...)``."""

    name: str = ""
    display_name: str = ""  # used in error messages; defaults to ``name``
    capabilities: set[str]
    auth: ConnectorAuth

    def __init__(
        self,
        *,
        base_url: str,
        http: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
        retries: int = 1,
        retry_backoff: float = 0.2,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = http or httpx.AsyncClient(timeout=timeout)
        self.retries = retries
        self.retry_backoff = retry_backoff

    # --- requests ---------------------------------------------------------------------------
    def headers(self, cred: ConnectorCredential) -> dict[str, str]:
        """Per-request auth headers. Override for API keys or extra headers."""
        return {"Authorization": f"Bearer {cred.tokens.access_token}", "Accept": "application/json"}

    def url(self, path: str) -> str:
        return path if path.startswith(("http://", "https://")) else f"{self.base_url}{path}"

    async def send(self, cred: ConnectorCredential, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Send a request and return the response whatever its status (see ``request``)."""
        method = method.upper()
        headers = {**self.headers(cred), **kwargs.pop("headers", {})}
        attempts = 1 + (self.retries if method in IDEMPOTENT_METHODS else 0)
        for attempt in range(attempts):
            last = attempt == attempts - 1
            try:
                resp = await self.http.request(method, self.url(path), headers=headers, **kwargs)
            except httpx.TransportError:
                if last:
                    raise
            else:
                if resp.status_code not in RETRYABLE_STATUS or last:
                    return resp
            await asyncio.sleep(self.retry_backoff * (attempt + 1))
        raise AssertionError("unreachable")  # pragma: no cover

    async def request(self, cred: ConnectorCredential, method: str, path: str, **kwargs: Any) -> Any:
        """Send a request; raise ``httpx.HTTPStatusError`` on 4xx/5xx (``map_error`` classifies
        it) and return the decoded JSON body, or None when the body is empty."""
        resp = await self.send(cred, method, path, **kwargs)
        resp.raise_for_status()
        return resp.json() if resp.content else None

    async def get(self, cred: ConnectorCredential, path: str, **kwargs: Any) -> Any:
        return await self.request(cred, "GET", path, **kwargs)

    async def post(self, cred: ConnectorCredential, path: str, **kwargs: Any) -> Any:
        return await self.request(cred, "POST", path, **kwargs)

    async def put(self, cred: ConnectorCredential, path: str, **kwargs: Any) -> Any:
        return await self.request(cred, "PUT", path, **kwargs)

    async def patch(self, cred: ConnectorCredential, path: str, **kwargs: Any) -> Any:
        return await self.request(cred, "PATCH", path, **kwargs)

    async def delete(self, cred: ConnectorCredential, path: str, **kwargs: Any) -> Any:
        return await self.request(cred, "DELETE", path, **kwargs)

    async def paginate(
        self,
        cred: ConnectorCredential,
        path: str,
        *,
        items: Callable[[Any], list[Any]],
        next_page: Callable[[Any], dict[str, Any] | None],
        method: str = "GET",
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        max_pages: int = 100,
    ) -> AsyncIterator[Any]:
        """Yield items page by page. ``next_page(body)`` returns the changes for the next request
        (merged into ``params`` for GET, into ``json`` otherwise) or None when done."""
        params, json = dict(params or {}), dict(json or {}) if json is not None else None
        for _ in range(max_pages):
            kwargs: dict[str, Any] = {"params": params} if params else {}
            if json is not None:
                kwargs["json"] = json
            body = await self.request(cred, method, path, **kwargs)
            for item in items(body):
                yield item
            changes = next_page(body)
            if not changes:
                return
            if method.upper() == "GET" or json is None:
                params.update(changes)
            else:
                json.update(changes)

    # --- errors -----------------------------------------------------------------------------
    def map_error(self, exc: Exception) -> ConnectorError:
        """HTTP status -> ``ConnectorError`` kind. 401: auth (reconnect); 429: rate_limited with
        Retry-After; 404: not_found; 408 and 5xx: transient; other 4xx: permanent (deadletter);
        network errors: transient. Override to special-case a provider's quirks."""
        label = self.display_name or self.name or type(self).__name__
        if isinstance(exc, ConnectorError):
            return exc
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            if status == 401:
                return ConnectorError("auth", f"{label} token rejected")
            if status == 429:
                return ConnectorError(
                    "rate_limited",
                    f"{label} rate limit",
                    retry_after=parse_retry_after(exc.response.headers.get("Retry-After")),
                )
            if status == 404:
                return ConnectorError("not_found", exc.response.text[:200])
            if status == 408 or status >= 500:
                return ConnectorError("transient", f"{label} {status}")
            return ConnectorError("permanent", f"{label} {status}: {exc.response.text[:200]}")
        if isinstance(exc, httpx.TransportError):
            return ConnectorError("transient", f"{label}: {exc!r}")
        return ConnectorError("transient", repr(exc))

    async def aclose(self) -> None:
        await self.http.aclose()
