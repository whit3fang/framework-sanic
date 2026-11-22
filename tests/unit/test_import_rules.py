"""Import-lint rules (build gate).

* ``sanic``/``sanic_ext`` may be imported only in ``http/adapters/sanic.py``.
* Core domain code imports no web framework, no infrastructure client and none of the outer
  layers (adapters, http, connectors, workers, bootstrap): everything external sits behind a port.
* Core never imports features (``framework_sanic.features``): the framework only delivers events.
* Features cannot import each other.
* Stubs (``framework_sanic.stubs``: fakes for tests and local dev) are never imported by production
  code. Only ``bootstrap.py`` (config-selected), the stubs themselves, the test kit
  (``framework_sanic.testing``), tools and tests may.
"""

from __future__ import annotations

import ast
from pathlib import Path

PKG = Path(__file__).resolve().parents[2] / "src" / "framework_sanic"
SANIC_ALLOWED = {PKG / "http" / "adapters" / "sanic.py"}
WEB_FRAMEWORKS = {
    "sanic",
    "sanic_ext",
    "fastapi",
    "starlette",
    "aiohttp",
    "flask",
    "django",
    "tornado",
    "quart",
}
CORE_FORBIDDEN = WEB_FRAMEWORKS | {"httpx", "requests", "redis", "sqlalchemy", "aiosqlite", "google"}
CORE_FORBIDDEN_INTERNAL = (
    "framework_sanic.adapters",
    "framework_sanic.http",
    "framework_sanic.connectors",
    "framework_sanic.workers",
    "framework_sanic.bootstrap",
    "framework_sanic.cli",
    "framework_sanic.features",
)


def _module_name(path: Path) -> str:
    rel = path.relative_to(PKG.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    mod = _module_name(path)
    is_pkg = path.name == "__init__.py"
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = mod.split(".")
                base = base[: len(base) - node.level + (1 if is_pkg else 0)]
                name = ".".join(base + ([node.module] if node.module else []))
                out.append(name)
                if not node.module:
                    out.extend(f"{name}.{a.name}" for a in node.names)
            elif node.module:
                out.append(node.module)
    return out


def _all_files() -> list[Path]:
    return sorted(PKG.rglob("*.py"))


def test_sanic_only_imported_in_adapter() -> None:
    offenders = []
    for f in _all_files():
        if f in SANIC_ALLOWED:
            continue
        for name in _imports(f):
            if name.split(".")[0] in {"sanic", "sanic_ext"}:
                offenders.append(f"{f.relative_to(PKG)}: {name}")
    assert not offenders, "sanic imported outside http/adapters/sanic.py:\n" + "\n".join(offenders)


def test_core_has_no_framework_or_infrastructure_imports() -> None:
    offenders = []
    for f in sorted((PKG / "core").rglob("*.py")):
        for name in _imports(f):
            if name.split(".")[0] in CORE_FORBIDDEN or name.startswith(CORE_FORBIDDEN_INTERNAL):
                offenders.append(f"{f.relative_to(PKG)}: {name}")
    assert not offenders, "core imports forbidden modules:\n" + "\n".join(offenders)


def test_http_layer_is_framework_neutral() -> None:
    offenders = []
    for f in sorted((PKG / "http").rglob("*.py")):
        if f in SANIC_ALLOWED:
            continue
        for name in _imports(f):
            if name.split(".")[0] in WEB_FRAMEWORKS:
                offenders.append(f"{f.relative_to(PKG)}: {name}")
    assert not offenders, "\n".join(offenders)


def _feature_roots() -> dict[str, Path]:
    roots: dict[str, Path] = {}
    for d in (PKG / "core" / "features").iterdir():
        if d.is_dir() and (d / "__init__.py").exists():
            roots[_module_name(d / "__init__.py")] = d
    for d in (PKG / "features").iterdir():
        if d.is_dir() and (d / "__init__.py").exists():
            roots[_module_name(d / "__init__.py")] = d
    for d in (PKG / "stubs" / "connector" / "features").iterdir():
        if d.is_dir() and (d / "__init__.py").exists():
            roots[_module_name(d / "__init__.py")] = d
    return roots


def test_features_do_not_import_each_other() -> None:
    roots = _feature_roots()
    assert {
        "framework_sanic.core.features.platform",
        "framework_sanic.features.call_logging",
        "framework_sanic.features.sms_logging",
    } <= set(roots)
    offenders = []
    for name, root in roots.items():
        others = [o for o in roots if o != name]
        for f in root.rglob("*.py"):
            for imp in _imports(f):
                if any(imp == o or imp.startswith(o + ".") for o in others):
                    offenders.append(f"{f.relative_to(PKG)}: {imp}")
    assert not offenders, "features import each other:\n" + "\n".join(offenders)


STUB_ALLOWED = {PKG / "bootstrap.py"}


def test_production_code_does_not_import_test_helpers() -> None:
    """``framework_sanic.testing`` needs pytest; nothing outside it may import it."""
    offenders = [
        f"{f.relative_to(PKG)}: {name}"
        for f in _all_files()
        if not f.is_relative_to(PKG / "testing")
        for name in _imports(f)
        if name == "framework_sanic.testing" or name.startswith("framework_sanic.testing.")
    ]
    assert not offenders, "\n".join(offenders)


def test_production_code_does_not_import_stubs() -> None:
    offenders = []
    for f in _all_files():
        if f in STUB_ALLOWED or f.is_relative_to(PKG / "stubs") or f.is_relative_to(PKG / "testing"):
            continue
        for name in _imports(f):
            if name == "framework_sanic.stubs" or name.startswith("framework_sanic.stubs."):
                offenders.append(f"{f.relative_to(PKG)}: {name}")
    assert not offenders, "production code imports stubs:\n" + "\n".join(offenders)
    assert (PKG / "stubs" / "dialpad.py").exists() and not (PKG / "adapters" / "dialpad" / "fake.py").exists()


def test_lint_detects_violations(tmp_path: Path) -> None:
    """The checker itself works (guards against a silently passing lint)."""
    bad = PKG / "core" / "_lint_probe.py"
    bad.write_text("import sanic\n")
    try:
        assert any(n == "sanic" for n in _imports(bad))
        try:
            test_core_has_no_framework_or_infrastructure_imports()
        except AssertionError:
            pass
        else:  # pragma: no cover
            raise AssertionError("lint did not detect a sanic import in core")
    finally:
        bad.unlink()
