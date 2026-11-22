"""ParentPlatform port: everything the framework needs from Dialpad."""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field


class OAuthTokens(BaseModel):
    access_token: str
    refresh_token: str | None = None
    expires_in: float | None = None
    scope: list[str] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)


class PlatformIdentity(BaseModel):
    company_id: str
    company_name: str = ""
    user_id: str
    is_admin: bool = False  # VERIFY: is_admin on the user object
    email: str | None = None
    name: str = ""


class TargetInfo(BaseModel):
    target_type: Literal["company", "office", "user", "department", "callcenter"]
    target_id: str
    name: str = ""
    parent_type: Literal["company", "office", "user", "department", "callcenter"] | None = None
    parent_id: str | None = None
    email: str | None = None
    is_admin: bool = False


class SubscriptionSpec(BaseModel):
    kind: Literal["call", "sms"]
    target_type: str
    target_id: str
    endpoint_id: str
    enabled: bool = True
    # call
    call_states: list[str] = Field(default_factory=list)
    group_calls_only: bool = False
    # sms
    direction: str = "all"
    include_internal: bool = False
    status: bool = False


class RemoteSubscription(BaseModel):
    id: str
    kind: Literal["call", "sms"]
    target_type: str | None = None
    target_id: str | None = None
    endpoint_id: str | None = None
    enabled: bool = True
    settings: dict[str, Any] = Field(default_factory=dict)


class PlatformAuthError(Exception):
    """Access token rejected (expired, revoked)."""


class PlatformRateLimited(Exception):
    def __init__(self, retry_after: float = 1.0) -> None:
        super().__init__(f"rate limited, retry after {retry_after}s")
        self.retry_after = retry_after


class ParentPlatform(Protocol):
    def authorize_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str: ...

    async def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> OAuthTokens: ...

    async def refresh(self, refresh_token: str) -> OAuthTokens: ...

    async def identify(self, access_token: str) -> PlatformIdentity: ...

    async def probe(self, access_token: str) -> bool:
        """Validity probe for a credential."""
        ...

    async def list_targets(self, access_token: str, company_id: str) -> list[TargetInfo]:
        """All targets with parent references (company, offices, users, departments, call centers)."""
        ...

    async def ensure_webhook(self, access_token: str, *, hook_url: str, secret: str) -> str:
        """Create (or find) the webhook endpoint for ``hook_url``; returns its endpoint id."""
        ...

    async def list_subscriptions(
        self, access_token: str, kind: Literal["call", "sms"]
    ) -> list[RemoteSubscription]: ...

    async def create_subscription(self, access_token: str, spec: SubscriptionSpec) -> RemoteSubscription: ...

    async def update_subscription(
        self, access_token: str, subscription_id: str, spec: SubscriptionSpec
    ) -> RemoteSubscription: ...

    async def delete_subscription(
        self, access_token: str, kind: Literal["call", "sms"], subscription_id: str
    ) -> None: ...
