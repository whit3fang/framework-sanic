from __future__ import annotations

from pydantic import BaseModel

from framework_sanic.core.ports.connector import UiManifest
from framework_sanic.core.services.company_service import AppState

from ..routes import RequestContext, route


class SessionIn(BaseModel):
    pass


class SessionOut(BaseModel):
    state: AppState
    company_id: str
    user_id: str
    is_admin: bool
    external: dict[str, bool]
    admin_notice: str | None = None
    session_token: str
    expires_at: float
    manifest: UiManifest
    dialpad_connect_url: str
    connector: str
    features: list[str]  # enabled features, so the UI can show only what is available


class LogoutOut(BaseModel):
    revoked: bool


@route("GET", "/session", auth="session")
async def get_session(req: SessionIn, ctx: RequestContext) -> SessionOut:
    """Reports the app state and exchanges the Dialpad id_token for a short-lived session token."""
    s = ctx.services
    principal = ctx.require_principal()
    status = await s.companies.status(principal)
    token, exp = s.sessions.issue(principal)
    return SessionOut(
        **status.model_dump(),
        session_token=token,
        expires_at=exp,
        manifest=s.connector.ui_manifest(),
        dialpad_connect_url=f"{s.settings.PUBLIC_BASE_URL.rstrip('/')}/oauth/dialpad/start",
        connector=s.connector.name,
        features=[c.name for c in s.features.contexts] if s.features else [],
    )


@route("POST", "/session/logout", auth="session")
async def logout(req: SessionIn, ctx: RequestContext) -> LogoutOut:
    principal = ctx.require_principal()
    if principal.session_id:
        await ctx.services.sessions.revoke(principal.session_id)
    return LogoutOut(revoked=bool(principal.session_id))
