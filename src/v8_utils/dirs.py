"""Per-user directories, at their XDG locations on every platform.

platformdirs maps macOS to ~/Library/{Application Support,Caches}. v8-utils
uses ~/.config, ~/.local/share and ~/.cache there as well (honouring the XDG_*
variables), so one set of paths works on both platforms, including for
sandboxes that mount them.
"""

from __future__ import annotations

from pathlib import Path

from platformdirs.unix import Unix

_APP = "v8-utils"


def config_dir() -> Path:
    return Path(Unix(_APP).user_config_dir)


def data_dir() -> Path:
    return Path(Unix(_APP).user_data_dir)


def cache_dir() -> Path:
    return Path(Unix(_APP).user_cache_dir)
