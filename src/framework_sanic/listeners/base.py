"""Listener base classes. Each listener is a feature: give it a ``name``, implement its method,
and register it under the ``framework_sanic.features`` entry point group."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

from framework_sanic.core.container import Services
from framework_sanic.core.domain.errors import ConfigurationError
from framework_sanic.core.features.context import FeatureContext
from framework_sanic.core.features.jobs import JobResult
from framework_sanic.core.intake import InboundEvent
from framework_sanic.core.services.session_service import Principal
from framework_sanic.http.routes import RequestContext, route

from .models import NoteIn, NoteOut


class _Listener:
    name: ClassVar[str] = ""
    #: Dialpad OAuth scopes the listener needs (checked at admin connect).
    scopes: ClassVar[tuple[str, ...]] = ()

    def setup(self, ctx: FeatureContext) -> None:
        if not self.name:
            raise ConfigurationError(f"{type(self).__name__} must set a feature `name`")
        self.services: Services = ctx.services
        ctx.require_scopes(*self.scopes)
        self._register(ctx)
        self.configure(ctx)

    def _register(self, ctx: FeatureContext) -> None:
        raise NotImplementedError

    def configure(self, ctx: FeatureContext) -> None:
        """Optional: register jobs (``ctx.job``), schedules, routes, settings or company data."""


class CallListener(_Listener):
    """Receives every Dialpad call event (each state change of each call leg) for targets with
    logging enabled, at least once and in any order. Return ``ACK`` when done, ``retry(...)`` to
    have it delivered again later, or ``deadletter(...)`` to give up."""

    def _register(self, ctx: FeatureContext) -> None:
        ctx.on_event("call", self.on_call)

    async def on_call(self, event: InboundEvent) -> JobResult:
        """``event.call`` is the parsed ``CallEvent``; ``CallActivity.from_event`` maps it."""
        raise NotImplementedError


class SmsListener(_Listener):
    """Receives every Dialpad SMS event, at least once. The text is not on ``event.sms``: decrypt
    it with ``await event.sms_text()`` when needed and never store it."""

    scopes: ClassVar[tuple[str, ...]] = ("message_content_export",)

    def _register(self, ctx: FeatureContext) -> None:
        ctx.on_event("sms", self.on_sms)

    async def on_sms(self, event: InboundEvent) -> JobResult:
        raise NotImplementedError


NoteHandler = Callable[[Principal, NoteIn], Awaitable[NoteOut]]


def notes_route(on_note: NoteHandler) -> Callable[..., Any]:
    """``POST /notes`` delivering to ``on_note`` (one note listener per deployment)."""

    @route("POST", "/notes", auth="session", status=202)
    async def add_note(req: NoteIn, ctx: RequestContext) -> NoteOut:
        """A note about a call, handed to the integration's note listener."""
        return await on_note(ctx.require_principal(), req)

    return add_note


class NoteListener(_Listener):
    """Receives notes users add to a call in the contact card (``POST /notes``, shown in the UI
    when a note listener is enabled). The framework stores nothing: append the note to the
    logged call, send it with a separate API call, or queue your own job (``ctx.job``)."""

    def _register(self, ctx: FeatureContext) -> None:
        ctx.route(notes_route(self.on_note))

    async def on_note(self, principal: Principal, note: NoteIn) -> NoteOut:
        raise NotImplementedError
