"""The worktree results and the `vt create --json` line built from them."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from v8_utils.api.worktree import Created


def test_vt_json_line_is_unchanged_by_the_typed_result():
    # The line callers parse, as vt printed it when create returned a dict.
    before = json.dumps(
        {"name": "wt", **{"path": Path("/src/v8/wt"), "builds": ["x64.release"]}},
        default=str,
    )
    result = Created(path=Path("/src/v8/wt"), builds=["x64.release"])
    assert json.dumps({"name": "wt", **asdict(result)}, default=str) == before
