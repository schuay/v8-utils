"""Core data models for pd — perf data analysis."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ChangePoint:
    """A detected performance change."""

    benchmark: str
    metric: str
    bot: str
    variant: str
    submetric: str
    commit_id: int
    prev_commit_id: int
    direction: str  # "improvement" | "regression"
    cohens_d: float
    pct_change: float
    p_value: float
    confidence: str  # "high" | "medium" | "low"
    seg_before_mean: float
    seg_after_mean: float
    candidates: list[tuple[int, float]] = field(default_factory=list)
    engine: str | None = None


@dataclass
class CommitDelta:
    """A before/after assessment of a single series at a known commit.

    Levels are taken locally from the commits hugging the target; the noise
    scale is estimated from the surrounding history, so the verdict survives
    even when only a point or two exist after the commit.
    """

    benchmark: str
    metric: str
    bot: str
    variant: str
    submetric: str
    engine: str | None
    snapped_commit_id: int
    before_level: float
    after_level: float
    step: float
    pct_change: float
    sigma: float
    snr: float
    z: float
    p_value: float
    n_before: int
    n_after: int
    history_n: int
    confidence: str  # "ok" | "tentative" | "low"
    transient: bool  # target is the newest measured commit; cannot confirm persistence
    spark: list[float] = field(default_factory=list)
    spark_split: int = 0  # index in spark where the after-segment begins
    p_adj: float = float("nan")
    significant: bool = False

    @property
    def direction(self) -> str:
        return "improvement" if self.step > 0 else "regression"


@dataclass
class CommitInfo:
    """Commit metadata from git log."""

    id: int
    hash: str
    date: str
    timestamp: int
    title: str
    author: str = ""


@dataclass
class AnalysisConfig:
    """Tuning parameters for PELT."""

    penalty: float = 3.0
    min_size: int = 2
    min_pct_change: float = 1.0  # percent
    min_effect_size: float = 0.5
    delay: int = 0
    refine_window: int = 3


@dataclass
class AtConfig:
    """Tuning parameters for the at-commit before/after assessment."""

    history: int = 20  # commits of pre-target history used to estimate sigma
    pre_cap: int = 8  # commits before the target used for the before-level
    post_cap: int = 8  # commits at/after the target used for the after-level
    min_pct_change: float = 1.0  # percent
    min_z: float = 2.0
    alpha: float = 0.05
    spark_pre: int = 12  # before-points shown in the sparkline
    spark_post: int = 8  # after-points shown in the sparkline
