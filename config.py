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
    js2_dir: Path = field(
        default=Path("~/JetStream2").expanduser(),
        metadata={
            _HELP: "Path to a JetStream2 checkout",
            _TOML: "~/JetStream2",
        },
    )
    js3_dir: Path = field(
        default=Path("~/JetStream3").expanduser(),
        metadata={
            _HELP: "Path to a JetStream3 checkout",
            _TOML: "~/JetStream3",
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

    # ── repos — related source repos for cross-reference ──────────────────────
    chromium_dir: Path | None = field(
        default=None,
        metadata={
            _SECTION: "repos — related source repos for cross-reference",
            _HELP: "Path to Chromium source checkout (for cpd sync chromium)",
            _OPT: True,
        },
    )
    jsc_dir: Path | None = field(
        default=None,
        metadata={
            _HELP: "Path to JavaScriptCore source (WebKit/Source/JavaScriptCore)",
            _OPT: True,
        },
    )
    spidermonkey_dir: Path | None = field(
        default=None,
        metadata={
            _HELP: "Path to SpiderMonkey source (gecko-dev/js/src)",
            _OPT: True,
        },
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

    _cache = Config(
        user=data.get("user"),
        poll_interval=int(data.get("poll_interval", 60)),
        chat_webhook=data.get("chat_webhook"),
        chat_service_account_email=data.get("chat_service_account_email"),
        chat_app_space=data.get("chat_app_space"),
        v8_out=_path("v8_out", Path("~/v8/out").expanduser()),
        js2_dir=_path("js2_dir", Path("~/JetStream2").expanduser()),
        js3_dir=_path("js3_dir", Path("~/JetStream3").expanduser()),
        default_build=data.get("default_build", "release"),
        perf_script=_path(
            "perf_script",
            Path("~/v8/tools/profiling/linux-perf-d8.py").expanduser(),
        ),
        chromium_dir=Path(data["chromium_dir"]).expanduser()
        if "chromium_dir" in data
        else None,
        jsc_dir=Path(data["jsc_dir"]).expanduser() if "jsc_dir" in data else None,
        spidermonkey_dir=Path(data["spidermonkey_dir"]).expanduser()
        if "spidermonkey_dir" in data
        else None,
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
