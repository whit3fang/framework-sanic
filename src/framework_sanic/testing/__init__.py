"""Test kit for integrations and adapters (``framework-sanic[testing]``: needs pytest).

* ``harness``: the whole framework in-process (memory adapters, fake Dialpad); the ``harness``
  pytest fixture comes from the auto-loaded plugin (``framework_sanic.testing.plugin``).
* ``connector_contract.ConnectorContract``: the suite every connector must pass.
* ``repository_contract.RepositoryContract``: the suite every storage adapter must pass.
* ``recording.CassetteTransport``: record real API responses once, replay them in CI.
"""

from __future__ import annotations

from .harness import (
    NOW_MS,
    FakeClock,
    Harness,
    build_harness,
    call_event,
    make_settings,
    sms_event,
    start_harness,
)

__all__ = [
    "NOW_MS",
    "FakeClock",
    "Harness",
    "build_harness",
    "call_event",
    "make_settings",
    "sms_event",
    "start_harness",
]
