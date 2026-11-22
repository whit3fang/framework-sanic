"""``framework-sanic check``: the repeatable gate an integration passes before it ships.

For each integration (``integrations/*`` in the framework repository, or the current directory
when it is an integration project), checks:

1. **SDK boundary**: code imports only ``framework_sanic.sdk*`` from the framework (tests may also
   use ``framework_sanic.testing*``). Everything else is internal and may change.
2. **Plugins load**: every connector / feature entry point in its ``pyproject.toml`` is installed
   and loads; connectors implement the Connector port and build a manifest.
3. **Wiring**: each feature, and the built-in logging features each connector supports, start
   with the framework (scopes, required connector ports, settings), using in-memory adapters.
4. **Contract**: each connector has a test class built on ``ConnectorContract``.
5. **Tests**: the integration's test suite passes (``--no-tests`` skips it).
"""

from __future__ import annotations

import ast
import logging
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

CONNECTOR_GROUP = "framework_sanic.connectors"
FEATURE_GROUP = "framework_sanic.features"
LOGGING_FEATURES = ("call_logging", "sms_logging")


def absolute_imports(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(), filename=str(path))
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((node.lineno, a.name) for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            out.append((node.lineno, node.module))
    return out


def boundary_violations(root: Path) -> list[str]:
    """Framework imports outside the public SDK (``src/``) or SDK + test kit (``tests/``)."""
    found: list[str] = []
    for sub, allowed in (
        ("src", ("framework_sanic.sdk",)),
        ("tests", ("framework_sanic.sdk", "framework_sanic.testing")),
    ):
        for f in sorted((root / sub).rglob("*.py")):
            for line, name in absolute_imports(f):
                if name != "framework_sanic" and not name.startswith("framework_sanic."):
                    continue
                if not any(name == a or name.startswith(a + ".") for a in allowed):
                    found.append(f"{f.relative_to(root)}:{line}: imports {name} (use framework_sanic.sdk)")
    return found


def contract_classes(root: Path) -> list[str]:
    names: list[str] = []
    for f in sorted((root / "tests").rglob("*.py")):
        for node in ast.walk(ast.parse(f.read_text(), filename=str(f))):
            if isinstance(node, ast.ClassDef) and any(
                (isinstance(b, ast.Name) and b.id == "ConnectorContract")
                or (isinstance(b, ast.Attribute) and b.attr == "ConnectorContract")
                for b in node.bases
            ):
                names.append(f"{f.relative_to(root)}::{node.name}")
    return names


@dataclass
class Report:
    name: str
    root: Path
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def fail(self, msg: str) -> None:
        self.failures.append(msg)

    def note(self, msg: str) -> None:
        self.notes.append(msg)


def _settings(**overrides: Any) -> Any:
    from framework_sanic.core.config import Settings

    base = {
        "_env_file": None,
        "PLATFORM_ADAPTER": "fake",
        "STORAGE_ADAPTER": "memory",
        "QUEUE_ADAPTER": "memory",
        "CACHE_URL": "memory://",
    }
    return Settings(**{**base, **overrides})


def _starts(connector: str, features: str) -> str | None:
    """None if the framework builds with this connector and these features, else the error."""
    from framework_sanic.bootstrap import build_services

    logging.disable(logging.WARNING)  # probes, not a running service: no startup warnings
    try:
        build_services(_settings(CONNECTOR=connector, FEATURES=features))
    except Exception as exc:  # noqa: BLE001 - reported to the user
        return f"{type(exc).__name__}: {exc}"
    finally:
        logging.disable(logging.NOTSET)
    return None


def check_integration(root: Path, *, run_tests: bool) -> Report:
    meta = tomllib.loads((root / "pyproject.toml").read_text())
    project = meta.get("project", {})
    report = Report(project.get("name", root.name), root)
    declared = project.get("entry-points", {})
    connectors = dict(declared.get(CONNECTOR_GROUP, {}))
    features = dict(declared.get(FEATURE_GROUP, {}))
    if not connectors and not features:
        report.fail(f"pyproject.toml declares no {CONNECTOR_GROUP} or {FEATURE_GROUP} entry points")

    # 1. SDK boundary
    for v in boundary_violations(root):
        report.fail(v)

    # 2. plugins are installed and load
    installed = {
        CONNECTOR_GROUP: {ep.name: ep for ep in entry_points(group=CONNECTOR_GROUP)},
        FEATURE_GROUP: {ep.name: ep for ep in entry_points(group=FEATURE_GROUP)},
    }
    loadable: dict[str, list[str]] = {CONNECTOR_GROUP: [], FEATURE_GROUP: []}
    for group, names in ((CONNECTOR_GROUP, connectors), (FEATURE_GROUP, features)):
        for name, target in names.items():
            ep = installed[group].get(name)
            if ep is None or ep.value != target:
                report.fail(f"entry point {name!r} ({group}) is not installed: run `uv sync`")
                continue
            try:
                ep.load()
            except Exception as exc:  # noqa: BLE001
                report.fail(f"entry point {name!r} does not load: {type(exc).__name__}: {exc}")
                continue
            loadable[group].append(name)

    # 3. wiring
    from framework_sanic.bootstrap import make_connector

    for name in loadable[CONNECTOR_GROUP]:
        try:
            connector = make_connector(_settings(CONNECTOR=name))
            connector.ui_manifest()
        except Exception as exc:  # noqa: BLE001
            report.fail(f"connector {name!r}: {type(exc).__name__}: {exc}")
            continue
        supported = [f for f in LOGGING_FEATURES if _starts(name, f) is None]
        report.note(f"connector {name!r}: supports {', '.join(supported) or 'none of the logging features'}")
    for name in loadable[FEATURE_GROUP]:
        with_connector = next(iter(loadable[CONNECTOR_GROUP]), "fake")
        error = _starts(with_connector, name)
        if error:
            report.fail(f"feature {name!r} does not start with connector {with_connector!r}: {error}")

    # 4. contract suite
    if connectors:
        classes = contract_classes(root)
        if not classes:
            report.fail(
                "no test class subclasses ConnectorContract (framework_sanic.testing.connector_contract)"
            )
        else:
            report.note(f"contract suite: {', '.join(classes)}")

    # 5. tests
    if run_tests and (root / "tests").is_dir():
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(root / "tests")],
            capture_output=True,
            text=True,
            check=False,
        )
        summary = (result.stdout.strip().splitlines() or ["(no output)"])[-1]
        if result.returncode != 0:
            report.fail(f"tests failed: {summary}")
            print(result.stdout[-4000:], file=sys.stderr)
        else:
            report.note(f"tests: {summary}")
    return report


def find_integrations(paths: list[str]) -> list[Path]:
    if paths:
        return [Path(p).resolve() for p in paths]
    cwd = Path.cwd()
    members = sorted(p.parent for p in (cwd / "integrations").glob("*/pyproject.toml"))
    if members:
        return members
    if (cwd / "pyproject.toml").exists() and "framework_sanic." in (cwd / "pyproject.toml").read_text():
        return [cwd]
    return []


def main(paths: list[str], *, run_tests: bool = True) -> int:
    roots = find_integrations(paths)
    if not roots:
        print("no integrations found (looked for integrations/*/pyproject.toml)", file=sys.stderr)
        return 1
    failed = 0
    for root in roots:
        if not (root / "pyproject.toml").exists():
            print(f"✗ {root}: no pyproject.toml", file=sys.stderr)
            failed += 1
            continue
        report = check_integration(root, run_tests=run_tests)
        mark = "✗" if report.failures else "✓"
        print(f"{mark} {report.name}  ({root})")
        for n in report.notes:
            print(f"    · {n}")
        for f in report.failures:
            print(f"    ✗ {f}")
        failed += bool(report.failures)
    print(f"\n{len(roots) - failed}/{len(roots)} integration(s) passed")
    return 1 if failed else 0
