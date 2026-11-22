"""Framework-neutral middleware pipeline, in this order:

request id and logging -> error mapping -> CORS/CSP -> webhook signature (webhook routes only)
-> id_token/session auth -> company context -> rate limit -> cache headers -> handler
"""

from __future__ import annotations

import fnmatch
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ValidationError

from framework_sanic.core.container import Services
from framework_sanic.core.domain.entities import Company
from framework_sanic.core.domain.errors import (
    DomainError,
    Forbidden,
    InvalidInput,
    NotConnected,
    NotFound,
    RateLimited,
    ServiceUnavailable,
    Unauthorized,
)
from framework_sanic.core.domain.records import Lane
from framework_sanic.core.ports.cache import CacheUnavailable
from framework_sanic.core.ports.connector import ConnectorError
from framework_sanic.core.ports.security import InvalidSignature
from framework_sanic.core.services.session_service import Principal

from .routes import HttpRequest, HttpResponse, RawResponse, RequestContext, RouteSpec

log = logging.getLogger("framework_sanic.http")

Next = Callable[[HttpRequest, RequestContext], Awaitable[HttpResponse]]
Middleware = Callable[[HttpRequest, RequestContext, Next], Awaitable[HttpResponse]]


def json_response(status: int, payload: Any, headers: dict[str, str] | None = None) -> HttpResponse:
    return HttpResponse(
        status=status, body=json.dumps(payload, default=str).encode(), headers=dict(headers or {})
    )


def error_response(status: int, code: str, message: str, request_id: str, **extra: Any) -> HttpResponse:
    return json_response(
        status, {"error": {"code": code, "message": message, "request_id": request_id, **extra}}
    )


# --- 1. request id and logging --------------------------------------------------------------
async def request_id_mw(req: HttpRequest, ctx: RequestContext, nxt: Next) -> HttpResponse:
    started = time.perf_counter()
    resp = await nxt(req, ctx)
    resp.headers["X-Request-Id"] = ctx.request_id
    log.info(
        "%s %s -> %s (%.1f ms) rid=%s company=%s",
        req.method,
        ctx.route.path,
        resp.status,
        (time.perf_counter() - started) * 1000,
        ctx.request_id,
        ctx.principal.company_id if ctx.principal else req.path_params.get("company_id"),
    )
    return resp


# --- 2. error mapping ----------------------------------------------------------------------
_STATUS: list[tuple[type[Exception], int]] = [
    (Unauthorized, 401),
    (Forbidden, 403),
    (NotFound, 404),
    (InvalidInput, 422),
    (RateLimited, 429),
    (NotConnected, 409),
    (ServiceUnavailable, 503),
]


async def error_mapping_mw(req: HttpRequest, ctx: RequestContext, nxt: Next) -> HttpResponse:
    webhook = ctx.route.auth == "webhook"
    try:
        return await nxt(req, ctx)
    except InvalidSignature as exc:
        return error_response(401, "invalid_signature", str(exc), ctx.request_id)
    except Unauthorized as exc:
        return error_response(401, exc.code, str(exc), ctx.request_id)
    except ValidationError as exc:
        if webhook:  # never 4xx a deliverable webhook event
            log.exception("webhook validation error rid=%s", ctx.request_id)
            return error_response(503, "unavailable", "temporarily unavailable", ctx.request_id)
        return error_response(
            422,
            "invalid_input",
            "invalid request",
            ctx.request_id,
            details=exc.errors(include_url=False, include_context=False),
        )
    except DomainError as exc:
        if webhook:
            log.exception("webhook error rid=%s", ctx.request_id)
            return error_response(503, "unavailable", "temporarily unavailable", ctx.request_id)
        status = next((s for t, s in _STATUS if isinstance(exc, t)), 400)
        headers = {"Retry-After": str(int(exc.retry_after) + 1)} if isinstance(exc, RateLimited) else {}
        resp = error_response(status, exc.code, str(exc), ctx.request_id)
        resp.headers.update(headers)
        return resp
    except ConnectorError as exc:
        status = {"auth": 409, "rate_limited": 429, "not_found": 404}.get(exc.kind, 502)
        return error_response(status, f"connector_{exc.kind}", str(exc), ctx.request_id)
    except Exception:
        log.exception("unhandled error rid=%s", ctx.request_id)
        # 5xx: Dialpad retries webhooks on 5xx (but not on 4xx).
        return error_response(500 if not webhook else 503, "internal", "internal error", ctx.request_id)


# --- 3. CORS / CSP -------------------------------------------------------------------------
def _origin_allowed(origin: str, allowed: list[str]) -> bool:
    return any(fnmatch.fnmatch(origin, pattern) for pattern in allowed)


def cors_csp_mw_factory(services: Services) -> Middleware:
    settings = services.settings
    frame_ancestors = " ".join(["'self'", *settings.frame_origins])
    csp = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; "
        f"connect-src 'self'; frame-ancestors {frame_ancestors}; base-uri 'none'; object-src 'none'"
    )

    async def cors_csp_mw(req: HttpRequest, ctx: RequestContext, nxt: Next) -> HttpResponse:
        origin = req.header("origin")
        cors: dict[str, str] = {}
        if origin and ctx.route.auth != "webhook" and _origin_allowed(origin, settings.ui_origins):
            cors = {
                "Access-Control-Allow-Origin": origin,
                "Vary": "Origin",
                "Access-Control-Allow-Headers": "Authorization, Content-Type, X-Dialpad-Id-Token",
                "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
                "Access-Control-Max-Age": "600",
            }
        if req.method == "OPTIONS":
            return HttpResponse(status=204, headers=cors)
        resp = await nxt(req, ctx)
        resp.headers.update(cors)
        resp.headers.setdefault("Content-Security-Policy", csp)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        return resp

    return cors_csp_mw


# --- 4. webhook signature ------------------------------------------------------------------
async def webhook_signature_mw(req: HttpRequest, ctx: RequestContext, nxt: Next) -> HttpResponse:
    if ctx.route.auth == "webhook":
        company_id = req.path_params.get("company_id", "")
        ctx.webhook_payload = ctx.services.webhooks.verify(company_id, req.body)
    return await nxt(req, ctx)


# --- 5. id_token / session auth ------------------------------------------------------------
async def _principal_from_id_token(services: Services, token: str) -> Principal:
    claims = await services.id_tokens.verify(token)
    is_admin = claims.is_admin
    if is_admin is None:
        # VERIFY: fall back to the stored User record when the id_token has no admin claim.
        from framework_sanic.core.domain.entities import User

        rec = await services.repo.get(User.KIND, (claims.company_id, claims.user_id))
        is_admin = bool(rec and rec.get("is_admin"))
    return Principal(
        company_id=claims.company_id, user_id=claims.user_id, is_admin=is_admin, email=claims.email
    )


async def auth_mw(req: HttpRequest, ctx: RequestContext, nxt: Next) -> HttpResponse:
    if ctx.route.auth in ("session", "admin"):
        authz = req.header("authorization") or ""
        id_token = req.header("x-dialpad-id-token") or req.query.get("id_token")
        if authz.lower().startswith("bearer "):
            ctx.principal = await ctx.services.sessions.verify(authz[7:].strip())
        elif id_token:
            ctx.principal = await _principal_from_id_token(ctx.services, id_token)
        else:
            raise Unauthorized("missing session token or id_token")
        if ctx.route.auth == "admin" and not ctx.principal.is_admin:
            raise Forbidden("admin only")
    return await nxt(req, ctx)


# --- 6. company context --------------------------------------------------------------------
async def company_context_mw(req: HttpRequest, ctx: RequestContext, nxt: Next) -> HttpResponse:
    if ctx.principal is not None:
        path_company = req.path_params.get("company_id")
        if path_company and path_company != ctx.principal.company_id:
            raise Forbidden("company mismatch")
        rec = await ctx.services.repo.get(Company.KIND, (ctx.principal.company_id,))
        ctx.company = Company.from_record(rec) if rec else None
    return await nxt(req, ctx)


# --- 7. rate limit (UI lane) ---------------------------------------------------------------
async def rate_limit_mw(req: HttpRequest, ctx: RequestContext, nxt: Next) -> HttpResponse:
    if ctx.principal is not None:
        try:
            wait = await ctx.services.limiter.acquire(
                f"ui:{ctx.principal.company_id}",
                per_minute=ctx.services.settings.UI_RATE_LIMIT_PER_MINUTE,
                lane=Lane.UI,
            )
        except CacheUnavailable:
            wait = 0.0
        if wait > 0:
            raise RateLimited("too many requests", retry_after=wait)
    return await nxt(req, ctx)


# --- 8. cache headers ----------------------------------------------------------------------
async def cache_headers_mw(req: HttpRequest, ctx: RequestContext, nxt: Next) -> HttpResponse:
    resp = await nxt(req, ctx)
    resp.headers.setdefault("Cache-Control", "no-store")
    return resp


# --- handler invocation --------------------------------------------------------------------
def _parse_input(spec: RouteSpec, req: HttpRequest) -> BaseModel:
    data: dict[str, Any] = dict(req.query)
    if req.body and spec.auth != "webhook" and req.method in ("POST", "PUT", "PATCH"):
        try:
            body = json.loads(req.body)
        except json.JSONDecodeError as exc:
            raise InvalidInput("request body must be JSON") from exc
        if not isinstance(body, dict):
            raise InvalidInput("request body must be a JSON object")
        data.update(body)
    data.update(req.path_params)
    return spec.input_model.model_validate(data)


async def invoke_handler(req: HttpRequest, ctx: RequestContext) -> HttpResponse:
    spec = ctx.route
    model = _parse_input(spec, req)
    result = await spec.handler(model, ctx)
    if isinstance(result, RawResponse):
        return HttpResponse(
            status=result.status,
            body=result.body.encode(),
            headers={**result.headers, **ctx.response_headers},
            content_type=result.content_type,
        )
    if isinstance(result, BaseModel):
        return HttpResponse(
            status=spec.status, body=result.model_dump_json().encode(), headers=dict(ctx.response_headers)
        )
    return json_response(spec.status, result, ctx.response_headers)


class Pipeline:
    def __init__(self, services: Services) -> None:
        self.services = services
        self.middlewares: list[Middleware] = [
            request_id_mw,
            error_mapping_mw,
            cors_csp_mw_factory(services),
            webhook_signature_mw,
            auth_mw,
            company_context_mw,
            rate_limit_mw,
            cache_headers_mw,
        ]

    async def dispatch(self, spec: RouteSpec, req: HttpRequest) -> HttpResponse:
        ctx = RequestContext(
            request_id=req.header("x-request-id") or uuid.uuid4().hex,
            services=self.services,
            http=req,
            route=spec,
        )

        async def call(index: int, r: HttpRequest, c: RequestContext) -> HttpResponse:
            if index == len(self.middlewares):
                return await invoke_handler(r, c)
            return await self.middlewares[index](r, c, lambda r2, c2: call(index + 1, r2, c2))

        return await call(0, req, ctx)
