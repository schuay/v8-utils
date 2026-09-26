"""The package's import layering.

    internals  <-  api  <-  render  <-  cli, mcp_tools, server

Frontends (cli, mcp_tools, server) import v8_utils.api, v8_utils.render and
their own package; render imports api and render; internals import none of
these. Everything a frontend or an outside consumer needs is therefore
reachable through api, and an internal module can be restructured without a
frontend noticing.

Imports are read from the syntax tree, lazy ones included, and relative
imports are resolved before the check. `from pkg import name` counts as an
import of pkg.name when that is a module, so `from .. import config` is an
import of v8_utils.config and not of v8_utils.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
PKG = SRC / "v8_utils"

FRONTENDS = (
    "v8_utils.cli",
    "v8_utils.mcp_tools",
    "v8_utils.server",
    "v8_utils.cli_deps",
)
RENDER = "v8_utils.render"
API = "v8_utils.api"
UPPER = (API, RENDER, *FRONTENDS)


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(SRC).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _is_module(name: str) -> bool:
    rel = Path(*name.split("."))
    return (SRC / rel).with_suffix(".py").is_file() or (
        SRC / rel / "__init__.py"
    ).is_file()


def _imports(path: Path) -> set[str]:
    """Every v8_utils module `path` imports."""
    package = _module_name(path).split(".")
    if path.name != "__init__.py":
        package.pop()
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.startswith("v8_utils"))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - node.level + 1]
                module = ".".join(base + ([node.module] if node.module else []))
            else:
                module = node.module or ""
            if not module.startswith("v8_utils"):
                continue
            for alias in node.names:
                sub = f"{module}.{alias.name}"
                found.add(sub if _is_module(sub) else module)
    return found


def _within(name: str, prefixes) -> bool:
    return any(name == p or name.startswith(p + ".") for p in prefixes)


def _allowed(importer: str, imported: str) -> bool:
    if _within(importer, FRONTENDS):
        own = next(p for p in FRONTENDS if _within(importer, (p,)))
        # server hosts mcp_tools; a frontend may also use its own package.
        extra = ("v8_utils.mcp_tools",) if importer == "v8_utils.server" else ()
        return _within(imported, (API, RENDER, own, *extra))
    if _within(importer, (RENDER,)):
        return _within(imported, (API, RENDER))
    if _within(importer, (API,)):
        return not _within(imported, (RENDER, *FRONTENDS))
    # Internals never import upward.
    return not _within(imported, UPPER)


def _violations() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for path in sorted(PKG.rglob("*.py")):
        importer = _module_name(path)
        bad = {m for m in _imports(path) if m != importer and not _allowed(importer, m)}
        if bad:
            out[importer] = bad
    return out


def test_imports_follow_the_layering():
    assert not _violations(), f"imports that break the layering: {_violations()}"


def test_the_checker_sees_relative_lazy_and_from_package_imports(tmp_path, monkeypatch):
    pkg = tmp_path / "v8_utils"
    (pkg / "cli").mkdir(parents=True)
    for name in ("__init__.py", "config.py", "cli/__init__.py"):
        (pkg / name).write_text("")
    (pkg / "cli" / "tool.py").write_text(
        "def f():\n    from .. import config\n    import v8_utils.config\n"
    )
    monkeypatch.setattr(__import__(__name__), "SRC", tmp_path)
    assert _imports(pkg / "cli" / "tool.py") == {"v8_utils.config"}
    assert not _allowed("v8_utils.cli.tool", "v8_utils.config")
    assert _allowed("v8_utils.cli.tool", "v8_utils.api.config")
    assert _allowed("v8_utils.render.pd", "v8_utils.api.pd")
    assert not _allowed("v8_utils.render.pd", "v8_utils.pd.commits")
    assert not _allowed("v8_utils.pd.commits", "v8_utils.api.pd")
