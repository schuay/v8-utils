"""Structured (JSON) serialization of change points.

The text report is for humans; this is the machine contract consumed by airc's
perf watcher, which keys and dedups change points programmatically and must read
the localization probability -- data that does not survive the rendered table.

Deliberately a hand-built shape, not `dataclasses.asdict(ChangePoint)`: the
dataclass is an internal detail that may grow fields or rename them, whereas this
dict is a wire contract. It also enriches each point with resolved git hashes
(the consumer process has no CommitStore) and a single `localization_confidence`
number -- the probability mass the candidate distribution puts on the chosen
breakpoint -- which is the gate the consumer applies before reporting a point.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .models import ChangePoint

if TYPE_CHECKING:
    from .commits import CommitStore


def _localization_confidence(cp: ChangePoint) -> float:
    """Probability the candidate distribution assigns to the chosen breakpoint.

    cp.commit_id is the refined breakpoint; cp.candidates is the normalized
    profile-likelihood distribution over nearby positions. The confidence that
    *this* commit is the change is the mass at commit_id, not the peak elsewhere
    -- a bimodal series should not read as confident. 0.0 when commit_id fell
    below the candidate min-probability floor (i.e. genuinely unlocalized).
    """
    for cid, prob in cp.candidates:
        if cid == cp.commit_id:
            return prob
    return 0.0


def changepoint_to_dict(
    cp: ChangePoint,
    commit_store: CommitStore | None,
    default_engine: str | None,
) -> dict:
    engine = cp.engine or default_engine

    def hash_of(commit_id: int) -> str:
        if commit_store and engine:
            info = commit_store.get(engine, commit_id)
            if info:
                return info.hash
        return ""

    def title_of(commit_id: int) -> str:
        if commit_store and engine:
            info = commit_store.get(engine, commit_id)
            if info:
                return info.title
        return ""

    return {
        "series": {
            "bot": cp.bot,
            "benchmark": cp.benchmark,
            "metric": cp.metric,
            "variant": cp.variant,
            "submetric": cp.submetric,
            "engine": engine or "",
        },
        "commit_id": cp.commit_id,
        "commit_hash": hash_of(cp.commit_id),
        "commit_title": title_of(cp.commit_id),
        "prev_commit_id": cp.prev_commit_id,
        "prev_commit_hash": hash_of(cp.prev_commit_id),
        "direction": cp.direction,
        "pct_change": cp.pct_change,
        "cohens_d": cp.cohens_d,
        "p_value": cp.p_value,
        # cp.confidence is the categorical series-noise tag (high/medium/low),
        # distinct from localization_confidence (how sure we are of the commit).
        "confidence": cp.confidence,
        "localization_confidence": _localization_confidence(cp),
        "seg_before_mean": cp.seg_before_mean,
        "seg_after_mean": cp.seg_after_mean,
        "candidates": [{"commit_id": cid, "prob": prob} for cid, prob in cp.candidates],
    }


def changepoints_to_json(
    results: list[ChangePoint],
    commit_store: CommitStore | None,
    default_engine: str | None,
) -> list[dict]:
    return [changepoint_to_dict(cp, commit_store, default_engine) for cp in results]
