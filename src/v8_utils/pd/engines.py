"""Engine definitions — maps engine names to repo paths and commit ID regexes."""

from __future__ import annotations

from pathlib import Path

ENGINES: dict[str, dict] = {
    "v8": {
        "id_regex": r"^ *Cr-Commit-Position:.*#([0-9]+)",
    },
    "chromium": {
        "id_regex": r"^ *Cr-Commit-Position:.*#([0-9]+)",
    },
    "jsc": {
        "id_regex": r"Canonical link:.*/([0-9]+)@",
        "path": "Source/JavaScriptCore",
    },
}


def get_src_dir(engine: str) -> Path | None:
    """Derive engine source directory from v8-utils config."""
    try:
        from ..config import load as load_v8_config

        cfg = load_v8_config()
    except Exception:
        return None

    entry = cfg.repos.get(engine)
    return entry.path if entry else None


def get_id_regex(engine: str) -> str | None:
    """Get the commit ID regex for an engine."""
    info = ENGINES.get(engine)
    return info["id_regex"] if info else None


def get_path_filter(engine: str) -> str | None:
    """Optional path inside the repo to restrict commits to (e.g. Source/JavaScriptCore)."""
    info = ENGINES.get(engine)
    return info.get("path") if info else None
