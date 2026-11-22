"""sms_logging: opt-in reference integration logic (``FEATURES=sms_logging``).

Logs each SMS to the connector once (insert-once metadata; the message text travels encrypted in
queue messages only and is never stored). Independent of ``call_logging``: enable either or both.
Requires a connector implementing ``SmsLogger``.
"""

from __future__ import annotations

from framework_sanic.core.domain.errors import ConfigurationError
from framework_sanic.core.features.context import FeatureContext
from framework_sanic.core.ports.queue import TOPIC_SYNC

from .handlers import SmsLogging
from .models import ENTITIES
from .port import SmsLogger


class SmsLoggingFeature:
    name = "sms_logging"

    def setup(self, ctx: FeatureContext) -> None:
        s = ctx.services
        if not isinstance(s.connector, SmsLogger):
            raise ConfigurationError(
                f"sms_logging requires a connector implementing SmsLogger; {s.connector.name!r} does not"
            )
        sms = SmsLogging(s)
        ctx.on_event("sms", sms.on_event)
        ctx.job(TOPIC_SYNC, "sync.sms")(sms.sync_sms)

        ctx.require_scopes("message_content_export")
        ctx.declare_config("ACTIVITY_TTL_DAYS", "UNKNOWN_CALLER_POLICY")
        ctx.declare_company_data(*ENTITIES)


feature = SmsLoggingFeature()

__all__ = ["SmsLogger", "SmsLoggingFeature", "feature"]
