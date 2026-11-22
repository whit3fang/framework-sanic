"""The public SDK and the integration tooling (scaffolding, the boundary check)."""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

import pytest

import framework_sanic.sdk as sdk
from framework_sanic.tools.check import boundary_violations, check_integration, contract_classes
from framework_sanic.tools.scaffold import generate

REPO = Path(__file__).resolve().parents[2]
INTEGRATIONS = sorted(p.parent for p in (REPO / "integrations").glob("*/pyproject.toml"))


def test_sdk_exports_resolve() -> None:
    mod = importlib.import_module("framework_sanic.sdk")
    missing = [n for n in mod.__all__ if not hasattr(mod, n)]
    assert not missing and len(set(mod.__all__)) == len(mod.__all__)


def test_sdk_covers_what_connectors_and_features_need() -> None:
    for name in (
        "RestConnector",
        "OAuth2Auth",
        "Connector",
        "FeatureContext",
        "InboundEvent",
        "SyncGuards",
        "PluginSettings",
        "load_settings",
        "route",
        "ACK",
        "retry",
        "deadletter",
        "CallListener",
        "SmsListener",
        "NoteListener",
        "CallActivity",
        "SmsActivity",
        "EntityMap",
    ):
        assert name in sdk.__all__


@pytest.mark.parametrize("root", INTEGRATIONS, ids=[p.name for p in INTEGRATIONS])
def test_integrations_only_use_the_sdk(root: Path) -> None:
    assert boundary_violations(root) == []


def test_boundary_check_detects_internal_imports(tmp_path: Path) -> None:
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "pkg" / "a.py").write_text(
        "from framework_sanic.sdk import Connector\nfrom framework_sanic.core.config import Settings\n"
        "import framework_sanic.bootstrap\nfrom framework_sanic.testing import Harness\n"
    )
    (tmp_path / "tests" / "test_a.py").write_text("from framework_sanic.testing import Harness\n")
    found = boundary_violations(tmp_path)
    assert len(found) == 3  # core.config, bootstrap and (in src/) the test kit
    assert all("src/pkg/a.py" in f for f in found)


def test_scaffold_generates_an_integration_package(tmp_path: Path) -> None:
    root = tmp_path / "zendesk"
    written = generate("zendesk", root, features=["tickets"])
    for f in written:
        if f.suffix == ".py":
            compile(f.read_text(), str(f), "exec")
    meta = tomllib.loads((root / "pyproject.toml").read_text())
    eps = meta["project"]["entry-points"]
    assert eps["framework_sanic.connectors"] == {"zendesk": "framework_sanic_zendesk:make"}
    assert eps["framework_sanic.features"] == {
        "zendesk.calls": "framework_sanic_zendesk.listeners.calls:feature",
        "zendesk.sms": "framework_sanic_zendesk.listeners.sms:feature",
        "zendesk.notes": "framework_sanic_zendesk.listeners.notes:feature",
        "zendesk.tickets": "framework_sanic_zendesk.features.tickets:feature",
    }
    assert boundary_violations(root) == []
    assert contract_classes(root) == ["tests/test_zendesk_connector.py::TestZendeskContract"]
    with pytest.raises(SystemExit):
        generate("zendesk", root, features=[])  # never overwrites
    with pytest.raises(SystemExit):
        generate("Bad-Name", tmp_path / "x", features=[])


def test_check_reports_uninstalled_plugins(tmp_path: Path) -> None:
    root = tmp_path / "zendesk"
    generate("zendesk", root, features=[])
    report = check_integration(root, run_tests=False)
    assert any("not installed" in f for f in report.failures)


def test_check_passes_for_hubspot() -> None:
    report = check_integration(REPO / "integrations" / "hubspot", run_tests=False)
    assert report.failures == []
    assert {
        "feature 'hubspot.calls': starts with connector 'hubspot'",
        "feature 'hubspot.notes': starts with connector 'hubspot'",
    } <= set(report.notes)
