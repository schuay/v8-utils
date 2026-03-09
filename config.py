"""v8-mcp configuration — loads ~/.config/v8-mcp/config.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_PATH = Path("~/.config/v8-mcp/config.toml").expanduser()


@dataclass
class Config:
    user: str | None = None
    chat_webhook: str | None = None
    poll_interval: int = 60  # seconds


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
        chat_webhook=data.get("chat_webhook"),
        poll_interval=int(data.get("poll_interval", 60)),
    )
    return _cache
