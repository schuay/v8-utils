"""v8-utils configuration — loads ~/.config/v8-utils/config.toml."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_PATH = Path("~/.config/v8-utils/config.toml").expanduser()


@dataclass
class Config:
    user: str | None = None
    poll_interval: int = 60

    # Google Chat — incoming webhook (simple, no auth required)
    chat_webhook: str | None = None

    # Google Chat — service account impersonation (supports direct user DMs)
    # chat_service_account_email: the service account associated with the Chat app
    # chat_app_space: DM space name, written by `pp chat-setup`, e.g. spaces/AAA...
    chat_service_account_email: str | None = None
    chat_app_space: str | None = None



_cache: Config | None = None


def load() -> Config:
    """Load and cache config from CONFIG_PATH. Missing file → defaults."""
    global _cache
    if _cache is not None:
        return _cache
    if not CONFIG_PATH.exists():
        _cache = Config()
        return _cache
    with CONFIG_PATH.open("rb") as f:
        data = tomllib.load(f)
    _cache = Config(
        user=data.get("user"),
        poll_interval=int(data.get("poll_interval", 60)),
        chat_webhook=data.get("chat_webhook"),
        chat_service_account_email=data.get("chat_service_account_email"),
        chat_app_space=data.get("chat_app_space"),
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


