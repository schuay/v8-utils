"""Breakpoint refinement via local MLE and candidate probability computation.

Ported from slipstream/analyzer.py — operates on per-commit mean arrays.
"""

from __future__ import annotations

import math

import numpy as np


def refine_breakpoints(
    means: np.ndarray,
    bkps: list[int],
    n_pts: int,
    window: int = 3,
) -> tuple[list[int], list[dict[int, float]]]:
    """Refine PELT breakpoints by minimising within-segment SSR in a ±window.

    Returns (refined_bkps, candidate_ssrs) where candidate_ssrs[i] maps
    array index → SSR for each candidate position around breakpoint i.
    """
    refined = []
    candidate_ssrs: list[dict[int, float]] = []
    prev_edge = 0

    for i, bk in enumerate(bkps):
        if bk >= n_pts:
            break
        next_edge = bkps[i + 1] if i + 1 < len(bkps) else n_pts
        best_bk = bk
        best_ssr = float("inf")
        ssrs: dict[int, float] = {}
        lo = max(prev_edge + 1, bk - window)
        hi = min(next_edge, bk + window + 1)

        for candidate in range(lo, hi):
            sb = means[prev_edge:candidate]
            sa = means[candidate:next_edge]
            if len(sb) < 1 or len(sa) < 1:
                continue
            ssr = float(np.sum((sb - sb.mean()) ** 2) + np.sum((sa - sa.mean()) ** 2))
            ssrs[candidate] = ssr
            if ssr < best_ssr:
                best_ssr = ssr
                best_bk = candidate

        refined.append(best_bk)
        candidate_ssrs.append(ssrs)
        prev_edge = best_bk

    return refined, candidate_ssrs


def candidate_probabilities(
    ssrs: dict[int, float],
    n_pts: int,
    commit_ids: list[int],
    min_prob: float = 0.01,
) -> list[tuple[int, float]]:
    """Compute candidate location probabilities from profile likelihood.

    P(bk=k) ∝ SSR(k)^(-n/2) under a Gaussian piecewise-constant model.
    """
    if not ssrs:
        return []

    positions = sorted(ssrs.keys())
    log_liks = [-(n_pts / 2) * math.log(max(ssrs[c], 1e-20)) for c in positions]
    max_ll = max(log_liks)
    probs = [math.exp(ll - max_ll) for ll in log_liks]
    total_p = sum(probs)

    return [
        (commit_ids[c], probs[j] / total_p)
        for j, c in enumerate(positions)
        if probs[j] / total_p >= min_prob
    ]
