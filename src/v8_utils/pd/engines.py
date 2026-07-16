"""Engine definitions — maps engine names to repo paths and commit ID regexes."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

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


def sync_engine(
    store, engine: str, *, since: str | None = None, fetch: bool = True
) -> int:
    """Populate `store` with an engine's commit metadata from its git checkout.

    The shared sync path behind both the `pd sync` CLI command and pd_detect's
    lazy auto-sync. Returns the number of commits processed, or 0 when the engine
    is unknown or its checkout is not configured/present -- a best-effort refresh
    that must never raise into a detect call, so a missing checkout degrades to
    "store stays as-is" rather than failing the query.

    Fetches origin/main first by default: populate() reads `git log origin/main`,
    so the checkout must be current for the sync to see new commits. A failed
    fetch is logged and the sync proceeds against whatever the checkout has.
    """
    id_regex = get_id_regex(engine)
    if not id_regex:
        log.warning("pd sync: unknown engine %r", engine)
        return 0
    src_dir = get_src_dir(engine)
    if not src_dir or not src_dir.is_dir():
        log.warning(
            "pd sync: no checkout for engine %r (check v8-utils config)", engine
        )
        return 0

    if fetch:
        res = subprocess.run(
            ["git", "-C", str(src_dir), "fetch", "origin", "main"],
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            log.warning("pd sync: fetch failed for %s: %s", engine, res.stderr.strip())

    return store.populate(
        engine,
        src_dir,
        id_regex,
        since=since or "6 months ago",
        path=get_path_filter(engine),
    )
