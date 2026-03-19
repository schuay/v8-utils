"""cpd — PELT change-point detection for benchmark time series."""

from .adaptor import Adaptor, discover
from .detect import detect
from .models import AnalysisConfig, ChangePoint, SeriesKey, SeriesPoint

__all__ = [
    "Adaptor",
    "AnalysisConfig",
    "ChangePoint",
    "SeriesKey",
    "SeriesPoint",
    "detect",
    "discover",
]
