"""Bundled pd adaptors load the way discover() loads them: by file path, with
no parent package. A relative import in one of them passes a normal import
and fails only there."""

from __future__ import annotations

from pathlib import Path

import pytest

from v8_utils.pd import adaptor

BUNDLED = sorted(
    p
    for p in (Path(adaptor.__file__).parent / "adaptors").glob("*.py")
    if not p.name.startswith("_")
)


@pytest.mark.parametrize("path", BUNDLED, ids=lambda p: p.stem)
def test_bundled_adaptor_loads_by_path(path):
    assert callable(adaptor._load_from_file(path))
