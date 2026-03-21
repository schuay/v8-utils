"""AB comparison of benchmark configurations.

Compares two "sides" (A and B) where any source field can differ
(variant, bot, benchmark, commit_id). Computes percentage deltas
with Welch's t-test and Benjamini-Hochberg FDR correction.
"""

from __future__ import annotations

import pandas as pd

from .adaptor import ensure_aggregated
from .stats import apply_fdr, welch_p


def compare_snapshots(
    df_a: pd.DataFrame,
    df_b: pd.DataFrame,
    key_cols: list[str],
    alpha: float = 0.05,
) -> pd.DataFrame:
    """Join two snapshots, compute deltas and significance.

    Args:
        df_a: Aggregated DataFrame for the A (base) side.
        df_b: Aggregated DataFrame for the B (experiment) side.
        key_cols: Columns to join on (the dimensions that are the same).
        alpha: Significance threshold after FDR correction.

    Returns:
        DataFrame with columns:
        *key_cols, a_mean, a_stdev, a_count, b_mean, b_stdev, b_count,
        pct_change, p_raw, p_adj, significant
        Sorted by |pct_change| descending.
    """
    df_a = ensure_aggregated(df_a)
    df_b = ensure_aggregated(df_b)

    # For snapshot comparison, pick the latest commit per key if multiple exist
    df_a = _latest_per_key(df_a, key_cols)
    df_b = _latest_per_key(df_b, key_cols)

    merged = pd.merge(
        df_a[key_cols + ["value", "stdev", "count"]],
        df_b[key_cols + ["value", "stdev", "count"]],
        on=key_cols,
        suffixes=("_a", "_b"),
        how="inner",
    )

    if merged.empty:
        return merged

    merged.rename(
        columns={
            "value_a": "a_mean",
            "stdev_a": "a_stdev",
            "count_a": "a_count",
            "value_b": "b_mean",
            "stdev_b": "b_stdev",
            "count_b": "b_count",
        },
        inplace=True,
    )

    # Compute percentage change
    merged["pct_change"] = merged.apply(
        lambda r: (r["b_mean"] - r["a_mean"]) / r["a_mean"] if r["a_mean"] else 0.0,
        axis=1,
    )

    # Welch's t-test per row
    merged["p_raw"] = merged.apply(
        lambda r: welch_p(
            r["a_mean"],
            r["a_stdev"],
            int(r["a_count"]),
            r["b_mean"],
            r["b_stdev"],
            int(r["b_count"]),
        ),
        axis=1,
    )

    # FDR correction
    fdr = apply_fdr(merged["p_raw"].tolist(), alpha=alpha)
    merged["p_adj"] = [adj for adj, _ in fdr]
    merged["significant"] = [sig for _, sig in fdr]

    # Sort by |pct_change| descending
    merged["abs_pct"] = merged["pct_change"].abs()
    merged.sort_values("abs_pct", ascending=False, inplace=True)
    merged.drop(columns=["abs_pct"], inplace=True)

    return merged.reset_index(drop=True)


def _latest_per_key(df: pd.DataFrame, key_cols: list[str]) -> pd.DataFrame:
    """Keep only the latest commit_id per key combination."""
    if "commit_id" not in df.columns:
        return df
    idx = df.groupby(key_cols, sort=False)["commit_id"].idxmax()
    return df.loc[idx]
