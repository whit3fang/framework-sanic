"""Builds the framework-neutral HTTP application: router (core + feature routes) and pipeline."""

from __future__ import annotations

from dataclasses import dataclass

from framework_sanic.core.container import Services

from .handlers import admin, contact, oauth, session, system, webhooks
from .middleware import Pipeline
from .routes import HttpRequest, HttpResponse, Router

CORE_ROUTE_MODULES = (system, webhooks, session, oauth, contact, admin)


@dataclass
class HttpApp:
    services: Services
    router: Router
    pipeline: Pipeline

    async def handle(self, req: HttpRequest) -> HttpResponse:
        """Adapter-agnostic dispatch (used by tests and as a fallback router)."""
        method = "GET" if req.method == "OPTIONS" else req.method
        matched = self.router.match(method, req.path)
        if matched is None:
            # OPTIONS preflight for any method on this path
            if req.method == "OPTIONS":
                for spec in self.router.routes:
                    m = spec.pattern.match(req.path)
                    if m:
                        req.path_params = m.groupdict()
                        return await self.pipeline.dispatch(spec, req)
            return HttpResponse(status=404, body=b'{"error":{"code":"not_found","message":"no such route"}}')
        spec, params = matched
        req.path_params = params
        return await self.pipeline.dispatch(spec, req)


def build_http_app(services: Services) -> HttpApp:
    router = Router()
    for module in CORE_ROUTE_MODULES:
        router.add_module(module)
    if services.features is not None:
        for fn in services.features.routes:
            router.add(fn)
    return HttpApp(services=services, router=router, pipeline=Pipeline(services))
