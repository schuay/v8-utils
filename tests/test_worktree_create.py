"""Tests for worktree.create's handling of V8's own worktree dependency setup."""

import subprocess

import pytest

from v8_utils import worktree


def _git(repo, *args):
    r = subprocess.run(
        ["git", *args], cwd=repo, check=False, capture_output=True, text=True
    )
    return r.returncode, r.stdout.strip()


@pytest.fixture
def checkout(tmp_path):
    """A gclient-style layout: <tmp>/.gclient and the main checkout at <tmp>/v8."""
    (tmp_path / ".gclient").write_text("solutions = []\n")
    main = tmp_path / "v8"
    main.mkdir()
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "tester@example.com"),
        ("config", "user.name", "Tester"),
        ("commit", "-q", "--allow-empty", "-m", "first"),
    ):
        assert _git(main, *args)[0] == 0
    return main


def test_create_disables_v8_deps_setup_before_gm_runs(checkout, monkeypatch):
    seen = {}

    def fake_setup_builds(wt_path, builds):
        # gm.py would run here; the flag must already be visible to it.
        seen["flag"] = _git(wt_path, "config", "--get", "v8.skip-worktree-deps")
        return []

    monkeypatch.setattr(worktree, "_create_symlinks", lambda *a: [])
    monkeypatch.setattr(worktree, "_setup_builds", fake_setup_builds)

    created = worktree.create(checkout, "wt-one")

    assert seen["flag"] == (0, "true")
    # Worktree scope only: the main checkout keeps V8's default behaviour.
    assert _git(checkout, "config", "--get", "v8.skip-worktree-deps")[0] == 1
    assert created.path == checkout.parent / "wt-one"
