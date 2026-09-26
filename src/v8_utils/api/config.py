"""The v8-utils config file: which one is read, and what it says."""

from .. import config as _config
from ..config import CONFIG_ENV, Config, Repo, configure, load, template


def config_path():
    """The file load() reads, after any override."""
    return _config.CONFIG_PATH


def update_chat_app_space(space: str) -> None:
    """Record the Google Chat DM space notifications go to."""
    _config.update_chat_app_space(space)


__all__ = [
    "CONFIG_ENV",
    "Config",
    "Repo",
    "config_path",
    "configure",
    "load",
    "template",
    "update_chat_app_space",
]
