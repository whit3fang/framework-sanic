"""Backend client libraries are optional extras: a plain install (core + memory adapters) works,
and selecting an adapter whose extra is missing fails with a message naming the extra."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from framework_sanic import bootstrap
from framework_sanic.core.domain.errors import ConfigurationError
from framework_sanic.testing import make_settings

PKG = Path(__file__).resolve().parents[2] / "src" / "framework_sanic"
OPTIONAL = {"google", "redis", "sqlalchemy", "aiosqlite"}
# The only modules allowed to import an optional library (each behind its adapter selection).
ALLOWED = {
    PKG / "adapters" / "cache" / "redis.py",
    PKG / "adapters" / "queue" / "pubsub.py",
    PKG / "adapters" / "kms" / "gcp_kms.py",
    PKG / "tools" / "dev.py",  # Redis ping, after the dev preflight check
}
ALLOWED_DIRS = (PKG / "adapters" / "storage" / "sqlalchemy", PKG / "adapters" / "storage" / "datastore")


def test_optional_libraries_are_imported_only_by_their_adapters() -> None:
    from tests.unit.test_import_rules import _imports

    offenders = [
        f"{f.relative_to(PKG)}: {name}"
        for f in sorted(PKG.rglob("*.py"))
        if f not in ALLOWED and not any(f.is_relative_to(d) for d in ALLOWED_DIRS)
        for name in _imports(f)
        if name.split(".")[0] in OPTIONAL
    ]
    assert not offenders, "optional backend libraries imported outside their adapters:\n" + "\n".join(
        offenders
    )


@pytest.mark.parametrize(
    ("overrides", "extra"),
    [
        ({"STORAGE_ADAPTER": "sqlalchemy"}, "sql"),
        ({"STORAGE_ADAPTER": "datastore"}, "gcp"),
        ({"QUEUE_ADAPTER": "pubsub"}, "gcp"),
        ({"CACHE_URL": "redis://localhost:6379/0"}, "redis"),
        ({"KMS_KEY": "projects/p/locations/l/keyRings/r/cryptoKeys/k"}, "gcp"),
    ],
)
def test_missing_extra_is_a_clear_configuration_error(
    overrides: dict[str, str], extra: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bootstrap, "_installed", lambda module: False)
    with pytest.raises(ConfigurationError, match=rf"needs the '{extra}' extra.*framework-sanic\[{extra}\]"):
        bootstrap.build_services(make_settings(**overrides))


def test_require_extra_passes_when_installed() -> None:
    bootstrap.require_extra("redis", "CACHE_URL")  # the repository environment has every extra


BLOCK_OPTIONAL = textwrap.dedent(
    """
    import importlib.abc, sys

    class Block(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in {"google", "redis", "sqlalchemy", "aiosqlite"}:
                raise ModuleNotFoundError(f"No module named {name!r} (blocked: plain install)")
            return None

    sys.meta_path.insert(0, Block())
    """
)


def test_plain_install_runs_on_memory_adapters() -> None:
    """In a process where no optional library can be imported: the SDK, the test kit and the whole
    framework on memory adapters work, and choosing a backend names its extra."""
    script = BLOCK_OPTIONAL + textwrap.dedent(
        """
        import asyncio
        import framework_sanic.sdk
        from framework_sanic.bootstrap import build_services, startup
        from framework_sanic.core.domain.errors import ConfigurationError
        from framework_sanic.http.adapters.sanic import create_app
        from framework_sanic.testing import build_harness, call_event, make_settings

        async def main():
            h = build_harness()
            await startup(h.services, schedules=False)
            await h.connect_company()
            assert (await h.webhook("c1", call_event("7"))).status == 200
            await h.drain()
            assert [r async for r in h.repo.query("FakeReceivedCall")], "call was not delivered"
            create_app(services=h.services, name="plain", embedded_workers=False)
            try:
                build_services(make_settings(STORAGE_ADAPTER="sqlalchemy"))
            except ConfigurationError as exc:
                assert "'sql' extra" in str(exc), exc
            else:
                raise AssertionError("sqlalchemy without the sql extra must fail")
            print("PLAIN-INSTALL-OK")

        asyncio.run(main())
        """
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)
    assert "PLAIN-INSTALL-OK" in result.stdout, result.stdout + result.stderr
