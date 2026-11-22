"""Framework-neutral route registry.

Handlers are ``async def handler(req: PydanticModel, ctx: RequestContext) -> PydanticModel`` and are
registered with ``@route(method, path, auth=...)``. Framework adapters (Sanic today, FastAPI
later) translate requests into ``HttpRequest`` and back from ``HttpResponse``.
"""

from __future__ import annotations

import re
import typing
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

from framework_sanic.core.container import Services
from framework_sanic.core.domain.entities import Company
from framework_sanic.core.domain.records import Lane
from framework_sanic.core.services.session_service import Principal

AuthMode = Literal["none", "webhook", "session", "admin"]


@dataclass
class HttpRequest:
    method: str
    path: str
    headers: dict[str, str]  # lower-cased names
    query: dict[str, str]
    body: bytes = b""
    path_params: dict[str, str] = field(default_factory=dict)
    client_ip: str | None = None

    def header(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name.lower(), default)


@dataclass
class HttpResponse:
    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    content_type: str = "application/json"


class RawResponse(BaseModel):
    """Handlers return this for non-JSON responses (redirects, small HTML pages)."""

    status: int = 200
    body: str = ""
    content_type: str = "text/html; charset=utf-8"
    headers: dict[str, str] = {}


class Redirect(RawResponse):
    def __init__(self, location: str, status: int = 302) -> None:
        super().__init__(status=status, body="", content_type="text/plain", headers={"Location": location})


@dataclass
class RequestContext:
    request_id: str
    services: Services
    http: HttpRequest
    route: RouteSpec
    principal: Principal | None = None
    company: Company | None = None
    webhook_payload: dict[str, Any] | None = None
    response_headers: dict[str, str] = field(default_factory=dict)
    lane: Lane = Lane.UI

    def require_principal(self) -> Principal:
        from framework_sanic.core.domain.errors import Unauthorized

        if self.principal is None:
            raise Unauthorized("authentication required")
        return self.principal


Handler = Callable[[Any, RequestContext], Awaitable[Any]]


@dataclass
class RouteSpec:
    method: str
    path: str
    auth: AuthMode
    handler: Handler
    input_model: type[BaseModel]
    output_model: type[BaseModel] | None
    status: int = 200
    name: str = ""

    @property
    def pattern(self) -> re.Pattern[str]:
        regex = re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", self.path)
        return re.compile(f"^{regex}$")

    @property
    def param_names(self) -> list[str]:
        return re.findall(r"\{(\w+)\}", self.path)


ROUTE_ATTR = "__route_spec__"


def route(
    method: str, path: str, *, auth: AuthMode = "session", status: int = 200
) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        hints = typing.get_type_hints(fn)
        params = [p for p in fn.__code__.co_varnames[: fn.__code__.co_argcount]]
        input_model = hints.get(params[0]) if params else None
        if not (isinstance(input_model, type) and issubclass(input_model, BaseModel)):
            raise TypeError(f"{fn.__name__}: first parameter must be annotated with a Pydantic model")
        ret = hints.get("return")
        output_model = ret if isinstance(ret, type) and issubclass(ret, BaseModel) else None
        setattr(
            fn,
            ROUTE_ATTR,
            RouteSpec(
                method=method.upper(),
                path=path,
                auth=auth,
                handler=fn,
                input_model=input_model,
                output_model=output_model,
                status=status,
                name=fn.__name__,
            ),
        )
        return fn

    return deco


class Router:
    def __init__(self) -> None:
        self.routes: list[RouteSpec] = []

    def add(self, fn: Callable[..., Any]) -> None:
        spec: RouteSpec | None = getattr(fn, ROUTE_ATTR, None)
        if spec is None:
            raise TypeError(f"{fn!r} is not decorated with @route")
        for existing in self.routes:
            if existing.method == spec.method and existing.path == spec.path:
                raise ValueError(f"duplicate route {spec.method} {spec.path}")
        self.routes.append(spec)

    def add_module(self, module: Any) -> None:
        for value in vars(module).values():
            if (
                callable(value)
                and hasattr(value, ROUTE_ATTR)
                and getattr(value, "__module__", None) == module.__name__
            ):
                self.add(value)

    def match(self, method: str, path: str) -> tuple[RouteSpec, dict[str, str]] | None:
        for spec in self.routes:
            if spec.method != method.upper():
                continue
            m = spec.pattern.match(path)
            if m:
                return spec, m.groupdict()
        return None

    def paths(self) -> set[str]:
        return {r.path for r in self.routes}
