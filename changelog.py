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
    # 222589f — multi-url
    "`pp show-job`, `show-results`, `watch` accept multiple job URLs",
    # 19bb805 — templates + multi-job
    "`pp create-job` supports templates and multi-job: `pp create-job -t js3 sp3 -c m1 linux`",
    # eddad5f, 266b427 — defaults + auto-detect + auto-watch
    "*create-job* defaults to _js3 sp3_ on _m1_ and auto-detects _exp-patch_ from your branch",
    "Jobs are *auto-watched* when chat integration is configured — no need for `-w`",
    # 644b010 — latest cached CI build
    "*create-job* uses the latest cached CI build — no more waiting for compiles",
    "`show-results --recent N` shows results for the N most recent completed jobs",
    "`pp cancel-job` cancels one or more Pinpoint jobs",
    "Multi-job operations _(`show-results`, `show-job`, `cancel-job`)_ now run concurrently",
    "`list-jobs` and `show-results` accept filter flags: `--patch`, `--benchmark`, `--bot`, `--status`, `--since`",
    "`list-jobs` and `show-results` now support `--patch=auto` to detect the CL from the current branch",
]

# ── Formatting ───────────────────────────────────────────────────────────────

_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[36m"
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

    unseen = ENTRIES[last_seen + 1 :]
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
