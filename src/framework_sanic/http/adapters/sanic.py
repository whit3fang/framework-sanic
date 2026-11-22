"""Sanic adapter: the ONLY module in the package allowed to import ``sanic`` / ``sanic_ext``.

It registers every RouteSpec natively, translates Sanic requests into ``HttpRequest`` and
``HttpResponse`` back into Sanic responses, and wires lifecycle (startup, embedded workers,
scheduler, shutdown). All behaviour lives in the framework-neutral pipeline.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from sanic import Request, Sanic
from sanic.response import HTTPResponse, file

from framework_sanic.bootstrap import build_services, build_workers, shutdown, startup
from framework_sanic.core.config import Settings
from framework_sanic.core.container import Services
from framework_sanic.http.app import HttpApp, build_http_app
from framework_sanic.http.routes import HttpRequest, HttpResponse, RouteSpec

log = logging.getLogger(__name__)


PACKAGED_UI_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "ui_dist")


def _sanic_path(path: str) -> str:
    return re.sub(r"\{(\w+)\}", r"<\1:str>", path)


def to_neutral(request: Request, path_params: dict[str, Any]) -> HttpRequest:
    return HttpRequest(
        method=request.method.upper(),
        path=request.path,
        headers={k.lower(): v for k, v in request.headers.items()},
        query={k: v[0] for k, v in request.args.items() if v},
        body=request.body or b"",
        path_params={k: str(v) for k, v in path_params.items()},
        client_ip=request.ip,
    )


def to_sanic(resp: HttpResponse) -> HTTPResponse:
    return HTTPResponse(
        body=resp.body, status=resp.status, headers=resp.headers, content_type=resp.content_type
    )


def _make_handler(http_app: HttpApp, spec: RouteSpec) -> Any:
    async def handler(request: Request, **path_params: Any) -> HTTPResponse:
        return to_sanic(await http_app.pipeline.dispatch(spec, to_neutral(request, path_params)))

    handler.__name__ = f"{spec.method.lower()}_{spec.name}"
    return handler


def _make_options(http_app: HttpApp, spec: RouteSpec) -> Any:
    async def options(request: Request, **path_params: Any) -> HTTPResponse:
        return to_sanic(await http_app.pipeline.dispatch(spec, to_neutral(request, path_params)))

    options.__name__ = f"options_{spec.name}"
    return options


def create_app(
    settings: Settings | None = None,
    *,
    services: Services | None = None,
    name: str = "integration_framework",
    embedded_workers: bool | None = None,
) -> Sanic[Any, Any]:
    settings = settings or (services.settings if services else Settings())
    services = services or build_services(settings)
    http_app = build_http_app(services)
    app = Sanic(name, configure_logging=True)
    app.config.KEEP_ALIVE_TIMEOUT = 5
    app.config.REQUEST_TIMEOUT = 5  # Dialpad times out webhooks after 5 s
    app.ctx.services = services
    app.ctx.http_app = http_app

    options_registered: set[str] = set()
    for spec in http_app.router.routes:
        uri = _sanic_path(spec.path)
        app.add_route(
            _make_handler(http_app, spec), uri, methods=[spec.method], name=f"{spec.method}_{spec.name}"
        )
        if uri not in options_registered and spec.auth != "webhook":
            options_registered.add(uri)
            app.add_route(
                _make_options(http_app, spec), uri, methods=["OPTIONS"], name=f"OPTIONS_{spec.name}"
            )

    # The prebuilt UI ships in the package (`pnpm -C ui build`); SERVE_UI_DIR overrides it.
    ui_dir = settings.SERVE_UI_DIR or PACKAGED_UI_DIR
    if ui_dir and os.path.isfile(
        os.path.join(ui_dir, "index.html")
    ):  # VERIFY: static hosting vs served by the service
        index = os.path.join(ui_dir, "index.html")

        async def ui_index(request: Request) -> HTTPResponse:
            resp = await file(index)
            resp.headers["Content-Security-Policy"] = (
                f"default-src 'self'; frame-ancestors 'self' {' '.join(settings.frame_origins)}; object-src 'none'"
            )
            return resp

        app.static("/app/assets", os.path.join(ui_dir, "assets"), name="ui_assets")
        app.add_route(ui_index, "/app/", methods=["GET"], name="ui_index")

    run_workers = settings.EMBEDDED_WORKERS if embedded_workers is None else embedded_workers

    @app.before_server_start
    async def _start(app: Sanic[Any, Any]) -> None:
        await startup(services, schedules=run_workers)
        if run_workers:
            app.ctx.workers = build_workers(services)
            await app.ctx.workers.start()

    @app.after_server_stop
    async def _stop(app: Sanic[Any, Any]) -> None:
        workers = getattr(app.ctx, "workers", None)
        if workers is not None:
            await workers.stop()
        await shutdown(services)

    return app


def serve(host: str = "0.0.0.0", port: int = 8000, settings: Settings | None = None) -> None:
    app = create_app(settings)
    app.run(host=host, port=port, single_process=True, access_log=False)
