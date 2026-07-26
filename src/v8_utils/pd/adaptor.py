"""Adaptor protocol and discovery for perf data sources."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import sys
from pathlib import Path
from typing import Protocol

import pandas as pd
from platformdirs import user_config_dir

_EP_GROUP = "pd.adaptors"
_ADAPTORS_DIR = Path(user_config_dir("v8-utils")) / "adaptors"

# Required columns in the DataFrame returned by fetch()
REQUIRED_COLUMNS = {
    "bot",
    "benchmark",
    "test",
    "variant",
    "commit_id",
    "value",
    "commit_time",
    "git_hash",
}
# Optional columns (present when data is pre-aggregated)
AGGREGATE_COLUMNS = {"stdev", "count"}
# Optional dimension columns
OPTIONAL_COLUMNS = {"submetric", "engine"}


class Adaptor(Protocol):
    """Protocol that perf data sources implement."""

    def fetch(
        self,
        since: str | None = None,
        until: str | None = None,
        **filters: str,
    ) -> pd.DataFrame:
        """Fetch data matching the filters as a flat DataFrame.

        Required columns:
            bot, benchmark, test, variant: str — dimension columns
            commit_id: int — monotonic commit ordinal
            value: float — measurement value (raw or pre-aggregated mean)
            commit_time: str — ISO date or datetime
            git_hash: str — git commit hash

        Optional columns (if data is pre-aggregated):
            stdev: float — standard deviation
            count: int — number of runs aggregated

        If stdev/count are absent, the caller assumes value is a raw
        sample and will aggregate per commit.
        """
        ...

    def distinct_values(self, column: str) -> list[str]:
        """Return every value a dimension column takes across the source.

        Optional: callers reach it via getattr and skip validation when an
        adaptor does not implement it. Used to turn a mistyped bot/benchmark/
        variant (which otherwise just matches zero rows) into an actionable
        error listing the real names. All-time on purpose -- the check must
        separate a typo from a valid-but-absent-in-window name.

        column: one of the dimension columns (bot, benchmark, test, variant).
        """
        ...


# Dimension filters whose values are a fixed vocabulary in the source, so a
# mistyped one is a nameable error rather than a legitimate empty result. `test`
# is excluded: callers treat it as a glob, not an exact name.
VALIDATED_DIMENSIONS = ("bot", "benchmark", "variant")


def check_dimension_values(
    adaptor,
    filters: dict[str, str],
    columns: tuple[str, ...] = VALIDATED_DIMENSIONS,
) -> None:
    """Raise ValueError naming any filter value the source does not have.

    Meant to run only once a query has already come back empty, so the extra
    DISTINCT lookups sit off the happy path. Each column is checked against its
    own value set independently, so a second bad filter cannot mask the first.
    Best-effort per column: an adaptor without distinct_values, a column with no
    known values, or a lookup that errors just skips that column rather than
    turning a soft empty result into a hard failure.
    """
    lookup = getattr(adaptor, "distinct_values", None)
    if lookup is None:
        return

    problems: list[tuple[str, str, list[str]]] = []
    for col in columns:
        if col not in filters:
            continue
        try:
            valid = lookup(col)
        except Exception:
            continue
        if not valid:
            continue
        if filters[col] not in valid:
            problems.append((col, filters[col], sorted(valid)))

    if problems:
        raise ValueError(
            "\n".join(
                f"Unknown {col} {val!r}. Available: {', '.join(valid)}"
                for col, val, valid in problems
            )
        )


def ensure_aggregated(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure the DataFrame has stdev/count columns.

    If already present, return as-is. Otherwise, aggregate raw values
    per (bot, benchmark, test, variant, commit_id) computing mean/stdev/count.
    """
    if df.empty:
        return df

    if "stdev" in df.columns and "count" in df.columns:
        return df

    group_cols = [
        "bot",
        "benchmark",
        "test",
        "variant",
        "commit_id",
        "commit_time",
        "git_hash",
    ]
    if "submetric" in df.columns:
        group_cols.insert(4, "submetric")
    if "engine" in df.columns:
        group_cols.append("engine")
    grouped = df.groupby(group_cols, sort=False)["value"]

    agg = grouped.agg(["mean", "std", "count"]).reset_index()
    agg.rename(columns={"mean": "value", "std": "stdev"}, inplace=True)
    agg["stdev"] = agg["stdev"].fillna(0.0)
    agg["count"] = agg["count"].astype(int)
    return agg


def _load_from_file(path: Path) -> callable:
    """Load a create() function from a Python file."""
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = mod
    spec.loader.exec_module(mod)
    return mod.create


def discover() -> dict[str, callable]:
    """Return {name: create_fn} for all available adaptors.

    Checks entry_points first, then config dir scripts, then bundled.
    """
    found: dict[str, callable] = {}

    # 1. Entry points from installed packages
    eps = importlib.metadata.entry_points()
    for ep in eps.select(group=_EP_GROUP):
        found[ep.name] = ep.load()

    # 2. Config dir scripts (~/.config/v8-utils/adaptors/*.py)
    if _ADAPTORS_DIR.is_dir():
        for py in sorted(_ADAPTORS_DIR.glob("*.py")):
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
