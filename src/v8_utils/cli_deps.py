"""Actionable errors for CLI entry points whose optional extra is missing.

The MCP server can skip a tool group whose extra is absent (see
mcp_tools.build_server), but a console script cannot: its module-scope imports
run before main() and fail with a bare ModuleNotFoundError traceback naming a
transitive package like scipy, which does not point at the fix.

Each CLI entry point goes through run_cli(), which turns that traceback into the
install command that resolves it.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

# Package that fails to import -> extra that provides it.
_PACKAGE_EXTRAS: dict[str, str] = {
    "scipy": "pinpoint",
    "protobuf": "pinpoint",
    "google.protobuf": "pinpoint",
    "dateparser": "pinpoint",
    "numpy": "analysis",
    "pandas": "analysis",
    "ruptures": "analysis",
    "google.auth": "gchat",
    "google.cloud.spanner": "spanner",
}


def _extra_for(module_name: str) -> str | None:
    """Map a failed import to the extra that supplies it, longest match first."""
    for candidate in sorted(_PACKAGE_EXTRAS, key=len, reverse=True):
        if module_name == candidate or module_name.startswith(candidate + "."):
            return _PACKAGE_EXTRAS[candidate]
    return None


def run_cli(module: str, attr: str = "main") -> None:
    """Import `module` and call `attr`, reporting a missing extra actionably."""
    import importlib

    try:
        entry = getattr(importlib.import_module(module), attr)
    except ImportError as exc:
        extra = _extra_for(getattr(exc, "name", "") or "")
        if extra is None:
            raise
        print(
            f"error: {exc}\n"
            f"This CLI needs the '{extra}' extra. Reinstall with:\n"
            f"  uv tool install --force v8-utils[all]",
            file=sys.stderr,
        )
        sys.exit(1)
    entry()


def _entry(module: str, attr: str = "main") -> Callable[[], None]:
    return lambda: run_cli(module, attr)


pp = _entry("v8_utils.pp")
jsb = _entry("v8_utils.jsb")
pd = _entry("v8_utils.pd.cli", "app")
