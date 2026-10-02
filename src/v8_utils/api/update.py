"""Version checks and interactive CLI updates."""

from .. import update as _update


def check_for_update() -> None:
    _update.check_for_update()


def install_command() -> list[str]:
    return _update.install_command()


def current_version() -> int:
    return _update.current_version()


__all__ = ["check_for_update", "install_command", "current_version"]
