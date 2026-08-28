"""The v8-utils and v8-utils-core distributions must stay two views of one set.

v8-utils (../pyproject.toml) installs everything so an interactive install needs
no extras; v8-utils-core (../packaging/core/pyproject.toml) splits the same
requirements into a light base plus extras for slim deployments. Nothing at
build time ties the two files together, so a dependency added to one and not the
other silently gives the deployed container a different dependency set than the
developer machine it was tested on.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement

_ROOT = Path(__file__).resolve().parent.parent
_FULL = _ROOT / "pyproject.toml"
_CORE = _ROOT / "packaging" / "core" / "pyproject.toml"


def _project(path: Path) -> dict:
    return tomllib.loads(path.read_text())["project"]


def _keys(requirements: list[str]) -> set[str]:
    """Normalize requirement strings so formatting differences do not register."""
    parsed = [Requirement(r) for r in requirements]
    return {
        f"{r.name}{','.join(sorted(e for e in r.extras))}{r.specifier}" for r in parsed
    }


@pytest.fixture(scope="module")
def full() -> dict:
    return _project(_FULL)


@pytest.fixture(scope="module")
def core() -> dict:
    return _project(_CORE)


def test_full_dependencies_are_core_base_plus_every_extra(full, core):
    extras = core["optional-dependencies"]
    # The `all` extra only re-exports the others; its recursive self-reference is
    # not a requirement of its own.
    union = set(core["dependencies"])
    for name, requirements in extras.items():
        if name != "all":
            union |= set(requirements)
    assert _keys(full["dependencies"]) == _keys(sorted(union))


def test_core_all_extra_covers_every_other_extra(core):
    extras = core["optional-dependencies"]
    referenced = {e for r in extras["all"] for e in Requirement(r).extras}
    assert referenced == set(extras) - {"all"}


def test_metadata_that_must_match(full, core):
    assert full["version"] == core["version"]
    assert full["requires-python"] == core["requires-python"]
    assert full["scripts"] == core["scripts"]
