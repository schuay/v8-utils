"""v8-utils configuration — loads ~/.config/v8-utils/config.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_PATH = Path("~/.config/v8-utils/config.toml").expanduser()


@dataclass
class Config:
    user: str | None = None
    poll_interval: int = 60

    # Google Chat — incoming webhook (simple, no auth required)
    chat_webhook: str | None = None

    # Google Chat — app / service account (supports direct user DMs)
    # chat_service_account_key: path to the service account JSON key file
    # chat_oauth_client_id / chat_oauth_client_secret: OAuth2 desktop client
    #   credentials (used by `pp chat-setup` to identify the user)
    # chat_app_space: DM space name written by `pp chat-setup`, e.g. spaces/AAA...
    chat_service_account_key: str | None = None
    chat_oauth_client_id: str | None = None
    chat_oauth_client_secret: str | None = None
    chat_app_space: str | None = None  # seconds


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
        chat_service_account_key=data.get("chat_service_account_key"),
        chat_oauth_client_id=data.get("chat_oauth_client_id"),
        chat_oauth_client_secret=data.get("chat_oauth_client_secret"),
        chat_app_space=data.get("chat_app_space"),
    )
    return _cache
