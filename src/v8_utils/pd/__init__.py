"""pd — perf data analysis for benchmark time series."""

from .adaptor import Adaptor, discover, ensure_aggregated
from .commits import CommitStore
from .compare import compare_snapshots
from .detect import detect_from_df, detect_series
from .models import AnalysisConfig, ChangePoint, CommitInfo

__all__ = [
    "Adaptor",
    "AnalysisConfig",
    "ChangePoint",
    "CommitInfo",
    "CommitStore",
    "compare_snapshots",
    "detect_from_df",
    "detect_series",
    "discover",
    "ensure_aggregated",
]
