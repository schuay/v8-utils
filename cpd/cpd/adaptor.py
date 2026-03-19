"""Adaptor protocol and discovery for data sources."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

from .models import SeriesKey, SeriesPoint

CONFIG_DIR = Path("~/.config/cpd").expanduser()
ADAPTORS_DIR = CONFIG_DIR / "adaptors"

_EP_GROUP = "cpd.adaptors"


class Adaptor(Protocol):
    """Protocol that data sources implement."""

    def list_series(self, **filters: str) -> Iterator[SeriesKey]:
        """Enumerate available time series, optionally filtered."""
        ...

    def fetch_series(
        self,
        key: SeriesKey,
        since: str | None = None,
        until: str | None = None,
    ) -> list[SeriesPoint]:
        """Fetch the time series for a given key, ordered by commit_id.

        Args:
            since: Optional YYYY-MM-DD lower bound (inclusive).
            until: Optional YYYY-MM-DD upper bound (inclusive).
        """
        ...


def _load_from_file(path: Path) -> callable:
    """Load a create() function from a Python file."""
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = mod
    spec.loader.exec_module(mod)
    return mod.create


def discover() -> dict[str, callable]:
    """Return {name: create_fn} for all available adaptors.

    Checks entry_points first, then config dir scripts.
    """
    found: dict[str, callable] = {}

    # 1. Entry points from installed packages
    eps = importlib.metadata.entry_points()
    for ep in eps.select(group=_EP_GROUP):
        found[ep.name] = ep.load()

    # 2. Config dir scripts (~/.config/cpd/adaptors/*.py)
    if ADAPTORS_DIR.is_dir():
        for py in sorted(ADAPTORS_DIR.glob("*.py")):
            if py.name.startswith("_"):
                continue
            found.setdefault(py.stem, _load_from_file(py))

    # 3. Bundled templates (fallback)
    bundled = Path(__file__).parent / "adaptors"
    if bundled.is_dir():
        for py in sorted(bundled.glob("*.py")):
            if py.name.startswith("_"):
                continue
            found.setdefault(py.stem, _load_from_file(py))

    return found
