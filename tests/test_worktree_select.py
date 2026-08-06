"""Tests for worktree selection in the repo_git MCP tools."""

import asyncio
import subprocess

import pytest
from mcp.server.fastmcp import FastMCP

from v8_utils import config
from v8_utils.mcp_tools import _shared, repo_git


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A 'demo' repo on branch main with one linked worktree.

    The worktree lives at <tmp>/wt-feature on branch feature/one, so its
    directory name and branch name differ -- lookup must accept either.
    """
    root = tmp_path / "demo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "tester@example.com")
    _git(root, "config", "user.name", "Tester")

    (root / "f.txt").write_text("shared line\nmain only\n")
    _git(root, "add", "f.txt")
    _git(root, "commit", "-qm", "first commit")

    wt = tmp_path / "wt-feature"
    _git(root, "worktree", "add", "-q", "-b", "feature/one", str(wt))
    (wt / "f.txt").write_text("shared line\nfeature only\n")
    _git(wt, "add", "f.txt")
    _git(wt, "commit", "-qm", "feature commit")

    monkeypatch.setattr(
        config,
        "_cache",
        config.Config(repos={"demo": config.Repo(path=root, desc="demo repo")}),
    )
    # Selection is process-global; keep tests independent of each other.
    _shared._active_worktree.clear()
    return root


@pytest.fixture
def mcp(repo):
    server = FastMCP("test")
    repo_git.register(server)
    return server


def _call(mcp, tool, **args):
    return asyncio.run(mcp.call_tool(tool, args)).content[0].text


def _grep(mcp, **args):
    args.setdefault("repo", "demo")
    args.setdefault("pattern", "only")
    return _call(mcp, "repo_git_grep", **args)


class TestResolution:
    def test_by_directory_name(self, mcp):
        _call(mcp, "repo_git_worktree_select", name="wt-feature", repo="demo")
        assert "feature only" in _grep(mcp)

    def test_by_branch_name(self, mcp):
        _call(mcp, "repo_git_worktree_select", name="feature/one", repo="demo")
        assert "feature only" in _grep(mcp)

    def test_unknown_name_lists_valid_ones(self, mcp):
        with pytest.raises(Exception) as exc:
            _call(mcp, "repo_git_worktree_select", name="nope", repo="demo")
        msg = str(exc.value)
        assert "wt-feature" in msg and "feature/one" in msg

    def test_default_is_main_checkout(self, mcp):
        assert "main only" in _grep(mcp)


class TestPrecedence:
    def test_param_overrides_selection(self, mcp):
        _call(mcp, "repo_git_worktree_select", name="wt-feature", repo="demo")
        # The main checkout is itself a worktree, so it is addressable by name.
        assert "main only" in _grep(mcp, worktree="demo")

    def test_param_does_not_change_selection(self, mcp):
        _call(mcp, "repo_git_worktree_select", name="wt-feature", repo="demo")
        _grep(mcp, worktree="demo")
        assert "feature only" in _grep(mcp)

    def test_select_with_no_name_returns_to_main(self, mcp):
        _call(mcp, "repo_git_worktree_select", name="wt-feature", repo="demo")
        out = _call(mcp, "repo_git_worktree_select", repo="demo")
        assert "wt-feature" in out  # reports what it left
        assert "main only" in _grep(mcp)

    def test_selection_is_per_repo(self, mcp, repo, monkeypatch):
        cfg = config.load()
        cfg.repos["other"] = config.Repo(path=repo, desc="same path, other alias")
        _call(mcp, "repo_git_worktree_select", name="wt-feature", repo="demo")
        assert "main only" in _grep(mcp, repo="other")


class TestBanner:
    def test_present_for_worktree(self, mcp):
        _call(mcp, "repo_git_worktree_select", name="wt-feature", repo="demo")
        assert _grep(mcp).startswith("[demo @ wt-feature | branch feature/one]")

    def test_absent_for_main_checkout(self, mcp):
        assert not _grep(mcp).startswith("[demo @")

    def test_present_for_per_call_override(self, mcp):
        out = _grep(mcp, worktree="wt-feature")
        assert out.startswith("[demo @ wt-feature | branch feature/one]")

    def test_present_on_empty_result(self, mcp):
        # The banner must survive the early-return paths, not just the happy one.
        _call(mcp, "repo_git_worktree_select", name="wt-feature", repo="demo")
        out = _grep(mcp, pattern="nomatchanywhere")
        assert out.startswith("[demo @ wt-feature")
        assert "No matches found." in out

    def test_applies_to_all_tools(self, mcp):
        _call(mcp, "repo_git_worktree_select", name="wt-feature", repo="demo")
        prefix = "[demo @ wt-feature"
        assert _call(mcp, "repo_git_show", repo="demo", regions="f.txt").startswith(
            prefix
        )
        assert _call(mcp, "repo_git_find", repo="demo", glob="*.txt").startswith(prefix)
        assert _call(mcp, "repo_git_log", repo="demo").startswith(prefix)
        assert _call(mcp, "repo_git_blame", repo="demo", path="f.txt").startswith(
            prefix
        )


class TestStaleSelection:
    def test_removed_worktree_errors_clearly(self, mcp, repo, tmp_path):
        _call(mcp, "repo_git_worktree_select", name="wt-feature", repo="demo")
        _git(repo, "worktree", "remove", "--force", str(tmp_path / "wt-feature"))
        with pytest.raises(Exception) as exc:
            _grep(mcp)
        msg = str(exc.value)
        assert "wt-feature" in msg
        assert "no longer exists" in msg


class TestWorktreeList:
    def test_lists_all_and_marks_none_when_unselected(self, mcp):
        out = _call(mcp, "repo_git_worktree_list", repo="demo")
        assert "wt-feature" in out and "feature/one" in out
        assert "No worktree selected" in out

    def test_marks_the_selected_worktree(self, mcp):
        _call(mcp, "repo_git_worktree_select", name="wt-feature", repo="demo")
        out = _call(mcp, "repo_git_worktree_list", repo="demo")
        marked = [ln for ln in out.splitlines() if ln.startswith("*")]
        assert len(marked) == 1
        assert "wt-feature" in marked[0]


class TestSelectReport:
    def test_reports_branch_and_clean_state(self, mcp):
        out = _call(mcp, "repo_git_worktree_select", name="wt-feature", repo="demo")
        assert "branch feature/one" in out
        assert "clean" in out

    def test_reports_uncommitted_count(self, mcp, tmp_path):
        (tmp_path / "wt-feature" / "f.txt").write_text("dirty\n")
        out = _call(mcp, "repo_git_worktree_select", name="wt-feature", repo="demo")
        assert "1 uncommitted file(s)" in out
