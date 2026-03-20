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
    min_pct_change: float = 0.01
    min_effect_size: float = 0.5
    delay: int = 0
    refine_window: int = 3
