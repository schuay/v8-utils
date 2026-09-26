"""pd: perf-data change-point detection, commit impact, and A/B comparison."""

from ..pd.adaptor import discover as adaptors
from ..pd.commits import CommitStore
from ..pd.engines import (
    ENGINES,
    get_id_regex,
    get_path_filter,
    get_src_dir,
    sync_engine,
)
from ..pd.models import ChangePoint, CommitDelta, CommitInfo
from ..pd.ops import (
    Comparison,
    Detection,
    Impact,
    commit_impact,
    compare,
    detect,
    detect_resolved,
    load_config,
    parse_date,
)
from ..pd.serialize import (
    Candidate,
    ResolvedChangePoint,
    Series,
    changepoints_to_payload,
)

__all__ = [
    "ENGINES",
    "Candidate",
    "ChangePoint",
    "CommitDelta",
    "CommitInfo",
    "CommitStore",
    "Comparison",
    "Detection",
    "Impact",
    "ResolvedChangePoint",
    "Series",
    "adaptors",
    "changepoints_to_payload",
    "commit_impact",
    "compare",
    "detect",
    "detect_resolved",
    "get_id_regex",
    "get_path_filter",
    "get_src_dir",
    "load_config",
    "parse_date",
    "sync_engine",
]
