"""Minimal changelog that shows unseen entries on first use.

Entries are appended to ENTRIES with ascending indices. The last-seen index
is stored in ~/.config/v8-utils/config.toml as `last_seen_changelog`.

Formatting mini-language (kept intentionally tiny):
  *bold*   →  bold text
  _dim_    →  dim text
  `code`   →  cyan text
"""

from __future__ import annotations

import re
import sys

import config

# ── Changelog entries ────────────────────────────────────────────────────────
# Append new entries at the end. Never remove or reorder.

ENTRIES: list[str] = [
    "*create-job* now defaults to _js3 sp3_ on _m1_ — just run `pp create-job`",
    "*create-job* auto-detects the Gerrit CL from your current branch as _exp-patch_",
    "*create-job* uses the latest cached CI build instead of HEAD (no more waiting for compiles)",
    "*create-job* supports multiple benchmarks × configs in one command: `pp create-job -t js3 sp3 -c m1 m4`",
    "Jobs are *auto-watched* when chat integration is configured — no need for `-w`",
]

# ── Formatting ───────────────────────────────────────────────────────────────

_BOLD  = "\033[1m"
_DIM   = "\033[2m"
_CYAN  = "\033[36m"
_RESET = "\033[0m"


def _format_entry(text: str, color: bool = True) -> str:
    if not color:
        text = re.sub(r"\*(.+?)\*", r"\1", text)
        text = re.sub(r"_(.+?)_", r"\1", text)
        text = re.sub(r"`(.+?)`", r"\1", text)
        return text
    text = re.sub(r"\*(.+?)\*", rf"{_BOLD}\1{_RESET}", text)
    text = re.sub(r"_(.+?)_", rf"{_DIM}\1{_RESET}", text)
    text = re.sub(r"`(.+?)`", rf"{_CYAN}\1{_RESET}", text)
    return text


# ── Display ──────────────────────────────────────────────────────────────────

def show_unseen() -> None:
    """Print unseen changelog entries to stderr, then update last-seen index."""
    if not sys.stderr.isatty():
        return
    if not ENTRIES:
        return

    cfg_data = _load_raw_config()
    raw = cfg_data.get("last_seen_changelog", -1)
    try:
        last_seen = int(raw)
    except (ValueError, TypeError):
        last_seen = -1

    unseen = ENTRIES[last_seen + 1:]
    if not unseen:
        return

    color = True  # stderr is a tty (checked above)
    header = f"{_BOLD}What's new:{_RESET}" if color else "What's new:"
    print(f"\n{header}", file=sys.stderr)
    for entry in unseen:
        print(f"  • {_format_entry(entry, color)}", file=sys.stderr)
    print(file=sys.stderr)

    config._set_value("last_seen_changelog", str(len(ENTRIES) - 1))


def _load_raw_config() -> dict:
    """Load the raw TOML dict (without going through Config dataclass)."""
    import tomllib
    if not config.CONFIG_PATH.exists():
        return {}
    with config.CONFIG_PATH.open("rb") as f:
        try:
            return tomllib.load(f)
        except Exception:
            return {}
