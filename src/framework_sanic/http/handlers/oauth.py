"""OAuth flows: Dialpad admin connect and external-service connect (user or company scope)."""

from __future__ import annotations

import html
from typing import Literal

from pydantic import BaseModel

from framework_sanic.core.domain.errors import Forbidden, InvalidInput, Unauthorized

from ..routes import RawResponse, Redirect, RequestContext, route


class Empty(BaseModel):
    pass


class CallbackIn(BaseModel):
    code: str | None = None
    state: str | None = None
    error: str | None = None


class ExternalStartIn(BaseModel):
    scope: Literal["user", "company"] = "user"


class AuthorizeOut(BaseModel):
    authorize_url: str


def _page(title: str, message: str, status: int = 200) -> RawResponse:
    body = (
        f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title></head>"
        f"<body style='font-family:system-ui;padding:2rem'><h1>{html.escape(title)}</h1><p>{html.escape(message)}</p>"
        "<p>You can close this window.</p></body></html>"
    )
    return RawResponse(status=status, body=body)


def _base(ctx: RequestContext) -> str:
    return ctx.services.settings.PUBLIC_BASE_URL.rstrip("/")


@route("GET", "/oauth/dialpad/start", auth="none")
async def dialpad_start(req: Empty, ctx: RequestContext) -> RawResponse:
    s = ctx.services
    state, challenge = await s.oauth_states.create({"kind": "dialpad"})
    url = s.platform.authorize_url(
        state=state, code_challenge=challenge, redirect_uri=f"{_base(ctx)}/oauth/dialpad/callback"
    )
    return Redirect(url)


@route("GET", "/oauth/dialpad/callback", auth="none")
async def dialpad_callback(req: CallbackIn, ctx: RequestContext) -> RawResponse:
    s = ctx.services
    if req.error or not req.code or not req.state:
        return _page("Connection failed", req.error or "missing code", 400)
    data = await s.oauth_states.consume(req.state)
    if data.get("kind") != "dialpad":
        raise Unauthorized("state does not belong to this flow")
    tokens = await s.platform.exchange_code(
        code=req.code, code_verifier=data["verifier"], redirect_uri=f"{_base(ctx)}/oauth/dialpad/callback"
    )
    identity = await s.platform.identify(tokens.access_token)
    if not identity.is_admin:  # VERIFY: is_admin semantics
        raise Forbidden("only a Dialpad admin can connect the integration")
    await s.companies.install(identity, tokens)
    return _page("Dialpad connected", f"{identity.company_name or identity.company_id} is now connected.")


@route("GET", "/oauth/external/start", auth="session")
async def external_start(req: ExternalStartIn, ctx: RequestContext) -> AuthorizeOut:
    s = ctx.services
    principal = ctx.require_principal()
    if req.scope == "company" and not principal.is_admin:
        raise Forbidden("only admins can connect a company-level credential")
    state, challenge = await s.oauth_states.create(
        {
            "kind": "external",
            "company_id": principal.company_id,
            "user_id": principal.user_id,
            "scope": req.scope,
        }
    )
    url = s.connector.auth.authorize_url(
        state=state, code_challenge=challenge, redirect_uri=f"{_base(ctx)}/oauth/external/callback"
    )
    return AuthorizeOut(authorize_url=url)


@route("GET", "/oauth/external/callback", auth="none")
async def external_callback(req: CallbackIn, ctx: RequestContext) -> RawResponse:
    s = ctx.services
    if req.error or not req.code or not req.state:
        return _page("Connection failed", req.error or "missing code", 400)
    data = await s.oauth_states.consume(req.state)
    if data.get("kind") != "external":
        raise Unauthorized("state does not belong to this flow")
    scope = data.get("scope")
    if scope not in ("user", "company"):
        raise InvalidInput("bad scope")
    tokens = await s.connector.auth.exchange_code(
        code=req.code, code_verifier=data["verifier"], redirect_uri=f"{_base(ctx)}/oauth/external/callback"
    )
    await s.external_credentials.save(
        data["company_id"],
        scope,
        data["user_id"],
        tokens,
        external_user_id=tokens.extra.get("external_user_id") or None,
    )
    await s.companies.audit(
        data["company_id"],
        "external_connect",
        data["user_id"],
        {"scope": scope, "connector": s.connector.name},
    )
    return _page(f"{s.connector.ui_manifest().display_name} connected", "Your account is now connected.")
