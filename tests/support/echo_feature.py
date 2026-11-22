"""Test-only integration feature: records what the framework delivers and can be told to fail.

Enabled in tests with ``FEATURES=tests.support.echo_feature`` (a module path). It handles only
``call`` events by default, so tests can check that subscriptions follow the registered handlers.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from framework_sanic.core.features.context import FeatureContext
from framework_sanic.core.features.jobs import ACK, JobResult, retry
from framework_sanic.core.intake import EventKind, InboundEvent


@dataclass
class EchoState:
    kinds: tuple[EventKind, ...] = ("call",)
    received: list[InboundEvent] = field(default_factory=list)
    texts: list[str | None] = field(default_factory=list)
    fail_times: int = 0


STATE = EchoState()


class EchoFeature:
    name = "echo"

    def setup(self, ctx: FeatureContext) -> None:
        async def handle(event: InboundEvent) -> JobResult:
            STATE.received.append(event)
            if event.kind == "sms":
                STATE.texts.append(await event.sms_text())
            if STATE.fail_times > 0:
                STATE.fail_times -= 1
                return retry(0.0, "told to fail")
            return ACK

        for kind in STATE.kinds:
            ctx.on_event(kind, handle)


feature = EchoFeature()
