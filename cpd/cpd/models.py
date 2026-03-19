"""Core data model for change-point detection."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SeriesPoint:
    """One commit's aggregated benchmark data."""

    commit_id: int
    mean: float
    stdev: float
    count: int
    commit_hash: str | None = None
    commit_date: str | None = None
    commit_title: str | None = None


@dataclass(frozen=True, eq=True)
class SeriesKey:
    """Identifies a unique time series."""

    source: str
    benchmark: str
    metric: str
    # Arbitrary source-specific dimensions (bot, variant, engine, etc.)
    # Not included in hash/eq — source+benchmark+metric should be unique.
    dimensions: dict[str, str] = field(default_factory=dict, hash=False, compare=False)


@dataclass
class ChangePoint:
    """A detected performance change."""

    series: SeriesKey
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


@dataclass
class AnalysisConfig:
    """Tuning parameters for PELT."""

    penalty: float = 3.0
    min_size: int = 2
    min_pct_change: float = 0.01
    min_effect_size: float = 0.5
    delay: int = 0
    refine_window: int = 3
