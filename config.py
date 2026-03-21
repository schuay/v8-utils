"""v8-utils configuration — loads ~/.config/v8-utils/config.toml."""

from __future__ import annotations

import dataclasses
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_PATH = Path("~/.config/v8-utils/config.toml").expanduser()

_SECTION = "section"  # metadata key for section headings
_HELP = "help"  # metadata key for description
_TOML = "toml"  # metadata key for the literal TOML default string (overrides computed)
_OPT = "optional"  # metadata key: if True, render commented-out in template


def _f(
    help: str,
    toml: str | None = None,
    section: str | None = None,
    optional: bool = False,
):
    """Shorthand for field(metadata=...)."""
    meta: dict = {_HELP: help, _OPT: optional}
    if toml is not None:
        meta[_TOML] = toml
    if section is not None:
        meta[_SECTION] = section
    return field(default=dataclasses.MISSING, metadata=meta)  # type: ignore[call-overload]


@dataclass
class Config:
    # ── General ──────────────────────────────────────────────────────────────
    user: str | None = field(
        default=None,
        metadata={
            _SECTION: "General",
            _HELP: "Your @chromium.org email — used as default for Pinpoint and other tools",
            _OPT: True,
        },
    )
    poll_interval: int = field(
        default=60,
        metadata={
            _HELP: "How often the watch daemon polls for job completion, in seconds",
        },
    )

    # ── Google Chat ───────────────────────────────────────────────────────────
    chat_webhook: str | None = field(
        default=None,
        metadata={
            _SECTION: "Google Chat",
            _HELP: "Incoming webhook URL for job-completion notifications (simplest setup)",
            _OPT: True,
        },
    )
    chat_service_account_email: str | None = field(
        default=None,
        metadata={
            _HELP: "Service account email for the Chat app — enables direct DMs to your account",
            _OPT: True,
        },
    )
    chat_app_space: str | None = field(
        default=None,
        metadata={
            _HELP: "DM space name — written automatically by `pp chat-setup`",
            _OPT: True,
        },
    )

    # ── jsb — JetStream bench runner ─────────────────────────────────────────
    v8_out: Path = field(
        default=Path("~/v8/out").expanduser(),
        metadata={
            _SECTION: "jsb — JetStream bench runner",
            _HELP: "Root of V8 build outputs; build dirs live here, e.g. out/release/d8",
            _TOML: "~/v8/out",
        },
    )
    default_build: str = field(
        default="release",
        metadata={
            _HELP: "Default build name used by jsb when -b is not specified",
        },
    )
    perf_script: Path = field(
        default=Path("~/v8/tools/profiling/linux-perf-d8.py").expanduser(),
        metadata={
            _HELP: "Path to linux-perf-d8.py, used by `jsb --perf`",
            _TOML: "~/v8/tools/profiling/linux-perf-d8.py",
        },
    )

    # ── repos — source repos accessible via MCP tools ─────────────────────────
    repos: dict[str, Path] = field(
        default_factory=lambda: {
            "js2": Path("~/JetStream2").expanduser(),
            "js3": Path("~/JetStream3").expanduser(),
        },
        metadata={
            _SECTION: "repos — source repos accessible via MCP tools",
            _HELP: 'Name → path mapping, e.g. jsc = "~/aspect/aspect-aspect/aspect"',
        },
    )

    # ── pd — perf data sources and analysis ──────────────────────────────
    sources: dict = field(
        default_factory=dict,
        metadata={
            _SECTION: "pd — perf data sources and analysis",
            _HELP: "Data sources for pd (see pd sources for available adaptors)",
        },
    )
    analysis: dict = field(
        default_factory=lambda: {
            "penalty": 3.0,
            "min_effect_size": 0.5,
            "min_pct_change": 0.01,
        },
        metadata={_HELP: "PELT analysis tuning parameters"},
    )


# ── Template generation ───────────────────────────────────────────────────────


def template() -> str:
    """Generate a fully-commented TOML template from the Config dataclass.

    Derived from the live Config definition — always current, never drifts.
    """
    lines = [
        "# v8-utils configuration",
        f"# Write to: {CONFIG_PATH}",
        "#",
        "# Run `pp config` or `jsb config` to regenerate this template.",
        "",
    ]

    for f in dataclasses.fields(Config):
        meta = f.metadata

        # Skip dict fields (TOML tables — users write these directly)
        if f.name in ("sources", "analysis"):
            continue

        # Repos table — render as [repos] section with examples
        if f.name == "repos":
            if section := meta.get(_SECTION):
                bar = "─" * max(0, 60 - len(section))
                lines += ["", f"# ── {section} {bar}"]
            if help_text := meta.get(_HELP):
                lines.append(f"# {help_text}")
            lines.append("[repos]")
            lines.append('js2 = "~/JetStream2"')
            lines.append('js3 = "~/JetStream3"')
            lines.append('# jsc = "~/aspect/aspect-aspect/aspect"')
            lines.append('# spidermonkey = "~/gecko-dev/js/src"')
            lines.append('# chromium = "~/chromium/src"')
            continue

        # Section heading
        if section := meta.get(_SECTION):
            bar = "─" * max(0, 60 - len(section))
            lines += ["", f"# ── {section} {bar}"]

        # Help comment
        if help_text := meta.get(_HELP):
            lines.append(f"# {help_text}")

        # Compute the TOML value string
        if (toml_str := meta.get(_TOML)) is not None:
            val = f'"{toml_str}"'
        elif f.default is None:
            val = '"..."'
        elif isinstance(f.default, str):
            val = f'"{f.default}"'
        elif isinstance(f.default, int):
            val = str(f.default)
        elif isinstance(f.default, Path):
            val = f'"{f.default}"'
        else:
            val = repr(f.default)

        line = f"{f.name} = {val}"
        if meta.get(_OPT):
            line = f"# {line}"
        lines.append(line)

    return "\n".join(lines)


# ── Loading ───────────────────────────────────────────────────────────────────

_cache: Config | None = None
_hinted = False


def load() -> Config:
    """Load and cache config from CONFIG_PATH. Missing file → defaults."""
    global _cache, _hinted
    if _cache is not None:
        return _cache
    if not CONFIG_PATH.exists():
        _cache = Config()
        if not _hinted and sys.stderr.isatty():
            _hinted = True
            print(
                f"hint: no config file found at {CONFIG_PATH}\n"
                f"      run `pp config` or `jsb config` to see available options",
                file=sys.stderr,
            )
        return _cache
    with CONFIG_PATH.open("rb") as f:
        try:
            data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ValueError(f"Failed to parse config file {CONFIG_PATH}: {e}") from e

    def _path(key: str, default: Path) -> Path:
        return Path(data[key]).expanduser() if key in data else default

    # Build repos dict: start with defaults, overlay [repos] table, migrate legacy keys
    repos: dict[str, Path] = {
        "js2": Path("~/JetStream2").expanduser(),
        "js3": Path("~/JetStream3").expanduser(),
    }
    if "repos" in data:
        repos.update({k: Path(v).expanduser() for k, v in data["repos"].items()})
    # Migrate legacy individual *_dir keys
    _LEGACY_REPO_KEYS = {
        "js2_dir": "js2",
        "js3_dir": "js3",
        "jsc_dir": "jsc",
        "spidermonkey_dir": "spidermonkey",
        "chromium_dir": "chromium",
    }
    for old_key, repo_name in _LEGACY_REPO_KEYS.items():
        if old_key in data and repo_name not in repos:
            repos[repo_name] = Path(data[old_key]).expanduser()

    _cache = Config(
        user=data.get("user"),
        poll_interval=int(data.get("poll_interval", 60)),
        chat_webhook=data.get("chat_webhook"),
        chat_service_account_email=data.get("chat_service_account_email"),
        chat_app_space=data.get("chat_app_space"),
        v8_out=_path("v8_out", Path("~/v8/out").expanduser()),
        default_build=data.get("default_build", "release"),
        perf_script=_path(
            "perf_script",
            Path("~/v8/tools/profiling/linux-perf-d8.py").expanduser(),
        ),
        repos=repos,
        sources=data.get("sources", {}),
        analysis=data.get(
            "analysis",
            {
                "penalty": 3.0,
                "min_effect_size": 0.5,
                "min_pct_change": 0.01,
            },
        ),
    )
    return _cache


def _set_value(key: str, value: str) -> None:
    """Write a single key = "value" line to the config file, creating it if needed."""
    global _cache
    _cache = None
    new_line = f'{key} = "{value}"'
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(new_line + "\n")
        return
    text = CONFIG_PATH.read_text()
    pattern = re.compile(rf"^{re.escape(key)}\s*=.*$", re.MULTILINE)
    if pattern.search(text):
        text = pattern.sub(new_line, text)
    else:
        if not text.endswith("\n"):
            text += "\n"
        text += new_line + "\n"
    CONFIG_PATH.write_text(text)


def update_chat_app_space(space: str) -> None:
    _set_value("chat_app_space", space)
