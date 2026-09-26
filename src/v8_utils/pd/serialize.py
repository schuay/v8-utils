"""Structured (JSON) serialization of change points.

The text report is for humans; this is the machine contract consumed by airc's
perf watcher, which keys and dedups change points programmatically and must read
the localization probability -- data that does not survive the rendered table.

The contract is ResolvedChangePoint, not `ChangePoint`: the latter is an
internal detail that may grow or rename fields. A resolved point carries the git
hashes looked up in the commit store, so a consumer needs no store of its own,
and a single `localization_confidence` number -- the probability mass the
candidate distribution puts on the chosen breakpoint -- which is the gate the
consumer applies before reporting a point. The JSON form is its `asdict`, so the
Python and wire shapes are one definition.

The payload is a paged envelope rather than a bare list. An unfiltered detect
run reports hundreds of points at ~1 KB each, and a consumer that caps tool
output by characters (airc caps at 50k) cuts mid-string, turning a valid
document into a decode error that loses the whole poll. A count-bounded page
plus an explicit `truncated` flag makes the loss visible and recoverable, and
`total` reports how much was left behind.

Paging needs a total order that does not depend on detection order, so
`_page_key` sorts by localization confidence first -- the consumer's publish
gate, so the points it can act on come first -- then by magnitude, then by the
series identity as a tiebreak. Detection is deterministic for fixed data, so
pages are coherent across calls; a page taken while new data lands may drop or
repeat a point, which the consumer's dedup absorbs and the next poll corrects.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
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


# Abbreviation length for the hashes on the wire. Every consumer resolves a rev
# against a real checkout before using it (airc's perf subscriber normalizes to
# the full SHA to key its commit threads), so the full 40 chars buy nothing and
# cost 56 per point across two fields. 12 is what the same code abbreviates to
# for display, and stays unambiguous well past V8's history.
_HASH_LEN = 12


def filter_by_localization(
    results: list[ChangePoint], min_confidence: float
) -> list[ChangePoint]:
    """Drop points whose breakpoint is not localized to at least `min_confidence`.

    A consumer that only acts on well-localized points (airc's perf watcher gates
    at 0.75, because a fuzzy point's commit wanders as data accrues and so cannot
    be a dedup key) would otherwise receive every fuzzy point, page it across the
    wire, and discard it. Applying the gate here instead means the points that
    cross are the points that matter -- and since detection is stateless and
    re-runs per page, it cuts the paging that dominates a poll.

    Confidence is derived from the candidate distribution alone, so this runs
    before commit hashes are resolved and never suppresses a point for being
    merely unsynced.
    """
    if min_confidence <= 0:
        return list(results)
    return [cp for cp in results if _localization_confidence(cp) >= min_confidence]


@dataclass(frozen=True)
class Series:
    bot: str
    benchmark: str
    metric: str
    variant: str
    submetric: str
    engine: str


@dataclass(frozen=True)
class Candidate:
    commit_id: int
    prob: float


@dataclass(frozen=True)
class ResolvedChangePoint:
    """A change point with its commits resolved against the commit store.

    Self-contained: a hash or title that could not be resolved is "". This is
    the structured result a consumer reads, and the field order is the JSON
    key order.
    """

    series: Series
    commit_id: int
    commit_hash: str
    commit_title: str
    prev_commit_id: int
    prev_commit_hash: str
    direction: str
    pct_change: float
    cohens_d: float
    p_value: float
    # The categorical series-noise tag (high/medium/low), distinct from
    # localization_confidence (how sure we are of the commit).
    confidence: str
    localization_confidence: float
    seg_before_mean: float
    seg_after_mean: float
    candidates: list[Candidate] = field(default_factory=list)


def resolve_changepoint(
    cp: ChangePoint,
    commit_store: CommitStore | None,
    default_engine: str | None,
) -> ResolvedChangePoint:
    engine = cp.engine or default_engine

    def hash_of(commit_id: int) -> str:
        if commit_store and engine:
            info = commit_store.get(engine, commit_id)
            if info:
                return info.hash[:_HASH_LEN]
        return ""

    def title_of(commit_id: int) -> str:
        if commit_store and engine:
            info = commit_store.get(engine, commit_id)
            if info:
                return info.title
        return ""

    return ResolvedChangePoint(
        series=Series(
            bot=cp.bot,
            benchmark=cp.benchmark,
            metric=cp.metric,
            variant=cp.variant,
            submetric=cp.submetric,
            engine=engine or "",
        ),
        commit_id=cp.commit_id,
        commit_hash=hash_of(cp.commit_id),
        commit_title=title_of(cp.commit_id),
        prev_commit_id=cp.prev_commit_id,
        prev_commit_hash=hash_of(cp.prev_commit_id),
        direction=cp.direction,
        pct_change=cp.pct_change,
        cohens_d=cp.cohens_d,
        p_value=cp.p_value,
        confidence=cp.confidence,
        localization_confidence=_localization_confidence(cp),
        seg_before_mean=cp.seg_before_mean,
        seg_after_mean=cp.seg_after_mean,
        candidates=[Candidate(commit_id=cid, prob=prob) for cid, prob in cp.candidates],
    )


def changepoint_to_dict(
    cp: ChangePoint,
    commit_store: CommitStore | None,
    default_engine: str | None,
) -> dict:
    return asdict(resolve_changepoint(cp, commit_store, default_engine))


def changepoints_to_json(
    results: list[ChangePoint],
    commit_store: CommitStore | None,
    default_engine: str | None,
) -> list[dict]:
    return [changepoint_to_dict(cp, commit_store, default_engine) for cp in results]


def _page_key(cp: ChangePoint) -> tuple:
    """Total order over change points, for coherent paging.

    Confidence descending first: it is the consumer's publish gate, so a page cut
    short still carries every point that could have been acted on. Magnitude
    breaks confidence ties, and the series identity breaks the rest -- without a
    final tiebreak two equal points could swap between pages and one of them
    would never be returned.
    """
    return (
        -_localization_confidence(cp),
        -abs(cp.pct_change),
        cp.bot,
        cp.benchmark,
        cp.metric,
        cp.variant,
        cp.submetric,
        cp.commit_id,
    )


def changepoints_to_payload(
    results: list[ChangePoint],
    commit_store: CommitStore | None,
    default_engine: str | None,
    limit: int,
    offset: int = 0,
) -> dict:
    """One page of change points, ordered by `_page_key`.

    `truncated` says whether points remain after this page, which is what lets a
    consumer drain the rest by advancing `offset` -- and what distinguishes a
    deliberate cut from the silent mid-document one a character cap would make.
    """
    ordered = sorted(results, key=_page_key)
    offset = max(offset, 0)
    page = ordered[offset : offset + limit] if limit > 0 else []
    return {
        "changepoints": changepoints_to_json(page, commit_store, default_engine),
        "total": len(ordered),
        "offset": offset,
        "returned": len(page),
        "truncated": offset + len(page) < len(ordered),
    }
