"""call_logging: opt-in reference integration logic (``FEATURES=call_logging``).

Logs each call to the connector (one external record per ``call_id``, order-independent fold,
recordings attached) and adds call notes. SMS logging is the separate ``sms_logging`` feature.

This is business logic, not framework: the framework only receives Dialpad events and delivers
them here via ``ctx.on_event``. An integration that needs different behaviour enables its own
feature instead (or alongside). Requires a connector implementing ``CallLogger``.
"""

from __future__ import annotations

from framework_sanic.core.domain.errors import ConfigurationError
from framework_sanic.core.features.context import FeatureContext
from framework_sanic.core.ports.queue import TOPIC_SYNC

from .models import ENTITIES
from .notes import add_note
from .port import CallLogger
from .stage1 import Stage1
from .stage2 import Stage2


class CallLoggingFeature:
    name = "call_logging"

    def setup(self, ctx: FeatureContext) -> None:
        s = ctx.services
        if not isinstance(s.connector, CallLogger):
            raise ConfigurationError(
                f"call_logging requires a connector implementing CallLogger; {s.connector.name!r} does not"
            )
        stage1 = Stage1(s)
        stage2 = Stage2(s)

        ctx.on_event("call", stage1.on_call)
        ctx.job(TOPIC_SYNC, "sync.call")(stage2.sync_call)
        ctx.job(TOPIC_SYNC, "sync.note")(stage2.sync_note)
        ctx.route(add_note)

        ctx.require_scopes("recordings_export", "calls:list")
        ctx.declare_config("ACTIVITY_TTL_DAYS", "UNKNOWN_CALLER_POLICY")
        ctx.declare_company_data(*ENTITIES)


feature = CallLoggingFeature()

__all__ = ["CallLogger", "CallLoggingFeature", "feature"]
