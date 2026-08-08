"""Tests for the repo_git MCP tools, focused on repo_git_blame."""

import asyncio
import subprocess

import pytest
from mcp.server.fastmcp import FastMCP

from v8_utils import config
from v8_utils.mcp_tools import repo_git
from v8_utils.mcp_tools.repo_git import _parse_blame_porcelain


# ══════════════════════════════════════════════════════════════════════════════
# Porcelain parser
# ══════════════════════════════════════════════════════════════════════════════


class TestParseBlamePorcelain:
    # Two-commit blame. Per git's --porcelain format, the full per-commit header
    # block is emitted only the first time a commit is seen; later lines of the
    # same commit carry just the hash header line and the content.
    BLOB = (
        f"{'a' * 40} 1 1 2\n"
        "author Alice\n"
        "author-mail <alice@example.com>\n"
        "author-time 1577923200\n"  # 2020-01-02 00:00:00 UTC
        "author-tz +0000\n"
        "committer Alice\n"
        "committer-mail <alice@example.com>\n"
        "committer-time 1577923200\n"
        "committer-tz +0000\n"
        "summary add greeting\n"
        "filename f.txt\n"
        "\thello\n"
        f"{'a' * 40} 2 2\n"
        "\tworld\n"
        f"{'b' * 40} 3 3 1\n"
        "author Bob\n"
        "author-mail <bob@example.com>\n"
        "author-time 1580601600\n"  # 2020-02-02 00:00:00 UTC
        "author-tz +0000\n"
        "committer Bob\n"
        "committer-mail <bob@example.com>\n"
        "committer-time 1580601600\n"
        "committer-tz +0000\n"
        "summary tweak\n"
        "filename f.txt\n"
        "\t!\n"
    )

    def test_lines(self):
        lines, _ = _parse_blame_porcelain(self.BLOB)
        assert lines == [
            ("aaaaaaaaa", 1, "hello"),
            ("aaaaaaaaa", 2, "world"),
            ("bbbbbbbbb", 3, "!"),
        ]

    def test_commit_metadata_cached_across_lines(self):
        # The second line reuses commit a without re-emitting its header; the
        # parser must still resolve its author/date/summary.
        _, commits = _parse_blame_porcelain(self.BLOB)
        assert commits["aaaaaaaaa"] == {
            "date": "2020-01-02",
            "author": "Alice",
            "summary": "add greeting",
        }
        assert commits["bbbbbbbbb"]["author"] == "Bob"
        assert commits["bbbbbbbbb"]["date"] == "2020-02-02"

    def test_author_tz_offset_applied(self):
        # A positive tz pushes the local date forward across midnight UTC.
        blob = (
            f"{'c' * 40} 1 1 1\n"
            "author Carol\n"
            "author-time 1577923200\n"  # 2020-01-02 00:00:00 UTC
            "author-tz +0900\n"
            "summary x\n"
            "filename f.txt\n"
            "\tx\n"
        )
        _, commits = _parse_blame_porcelain(blob)
        assert commits["ccccccccc"]["date"] == "2020-01-02"

    def test_empty_input(self):
        assert _parse_blame_porcelain("") == ([], {})


# ══════════════════════════════════════════════════════════════════════════════
# repo_git_blame tool (integration against a throwaway git repo)
# ══════════════════════════════════════════════════════════════════════════════


def _git(repo, *args, date=None):
    env = None
    if date is not None:
        env = {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=({**_base_env(), **env} if env else None),
    )


def _base_env():
    import os

    return os.environ.copy()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A git repo named 'demo' with a 10-line file authored by 'Tester'.

    Lines 1-3 come from the first commit, lines 4-10 from the second, so blame
    spans two commits and the legend has two entries.
    """
    root = tmp_path / "demo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "tester@example.com")
    _git(root, "config", "user.name", "Tester")

    f = root / "f.txt"
    f.write_text("line1\nline2\nline3\n")
    _git(root, "add", "f.txt")
    _git(root, "commit", "-qm", "first commit", date="2020-01-02T00:00:00 +0000")

    f.write_text("".join(f"line{i}\n" for i in range(1, 11)))
    _git(root, "add", "f.txt")
    _git(root, "commit", "-qm", "second commit", date="2021-06-07T00:00:00 +0000")

    monkeypatch.setattr(
        config,
        "_cache",
        config.Config(repos={"demo": config.Repo(path=root, desc="demo repo")}),
    )
    return root


@pytest.fixture
def mcp(repo):
    server = FastMCP("test")
    repo_git.register(server)
    return server


def _blame(mcp, **args):
    args.setdefault("repo", "demo")
    args.setdefault("path", "f.txt")
    result = asyncio.run(mcp.call_tool("repo_git_blame", args))
    return result.content[0].text


class TestRepoGitBlame:
    def test_basic_range(self, mcp):
        out = _blame(mcp, start=1, limit=3)
        body, _, legend = out.partition("Commits:")
        body_lines = body.strip().splitlines()
        assert len(body_lines) == 3
        # "<line#> <9-char hash> <content>"
        assert body_lines[0].split()[0] == "1"
        assert body_lines[0].endswith("line1")
        hashes = {ln.split()[1] for ln in body_lines}
        assert all(len(h) == 9 for h in hashes)
        # Lines 1-3 are all from the first commit, so one legend entry.
        assert legend.count("\n  ") == 1
        assert "2020-01-02" in legend
        assert "Tester" in legend
        assert "first commit" in legend

    def test_window_truncation_and_continuation(self, mcp):
        # File has 10 lines; a window of 3 must stop at 3 and point onward.
        out = _blame(mcp, start=1, limit=3)
        assert "continue at start=4" in out
        assert "showing lines 1-3" in out

    def test_continuation_resumes_correctly(self, mcp):
        out = _blame(mcp, start=4, limit=3)
        body = out.split("Commits:")[0].strip().splitlines()
        assert [ln.split()[0] for ln in body] == ["4", "5", "6"]
        assert body[0].endswith("line4")
        # Lines 4-6 belong to the second commit.
        assert "second commit" in out
        assert "2021-06-07" in out

    def test_no_continuation_when_window_covers_rest(self, mcp):
        # limit beyond EOF returns all remaining lines with no continuation hint.
        out = _blame(mcp, start=1, limit=500)
        body = out.split("Commits:")[0].strip().splitlines()
        assert len(body) == 10
        assert "more follow" not in out
        # Both commits appear in the legend.
        assert "first commit" in out
        assert "second commit" in out

    def test_two_commit_legend(self, mcp):
        out = _blame(mcp, start=1, limit=10)
        legend = out.split("Commits:")[1]
        assert legend.count("\n  ") == 2

    def test_ref_pins_history(self, mcp, repo):
        head = subprocess.run(
            ["git", "rev-parse", "HEAD~1"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        # At the first commit the file had only 3 lines, all "first commit".
        out = _blame(mcp, start=1, limit=10, ref=head)
        body = out.split("Commits:")[0].strip().splitlines()
        assert len(body) == 3
        assert "first commit" in out
        assert "second commit" not in out

    def test_invalid_start_rejected(self, mcp):
        with pytest.raises(Exception, match="start must be >= 1"):
            _blame(mcp, start=0, limit=3)

    def test_out_of_range_start_errors(self, mcp):
        with pytest.raises(Exception, match="git blame failed"):
            _blame(mcp, start=9999, limit=3)

    def test_unknown_repo_rejected(self, mcp):
        with pytest.raises(Exception, match="Unknown repo"):
            _blame(mcp, repo="nope", start=1, limit=3)


# ══════════════════════════════════════════════════════════════════════════════
# repo_git_log tool
# ══════════════════════════════════════════════════════════════════════════════


def _log(mcp, **args):
    args.setdefault("repo", "demo")
    result = asyncio.run(mcp.call_tool("repo_git_log", args))
    return result.content[0].text


class TestRepoGitLog:
    def test_basic(self, mcp):
        out = _log(mcp)
        assert "first commit" in out
        assert "second commit" in out

    def test_author_filter_matches(self, mcp):
        out = _log(mcp, author="Tester")
        assert "first commit" in out
        assert "second commit" in out

    def test_author_filter_excludes(self, mcp):
        out = _log(mcp, author="Nobody")
        assert out == "No commits found."

    def test_since_bounds_lower(self, mcp):
        # Only the 2021 commit is after this cutoff.
        out = _log(mcp, since="2021-01-01")
        assert "second commit" in out
        assert "first commit" not in out

    def test_until_bounds_upper(self, mcp):
        # Only the 2020 commit is before this cutoff.
        out = _log(mcp, until="2021-01-01")
        assert "first commit" in out
        assert "second commit" not in out

    def test_truncation_hint(self, mcp):
        out = _log(mcp, limit=1)
        assert "truncated — showing first 1 commits" in out
        # Newest commit is shown; the older one is dropped.
        assert "second commit" in out
        assert "first commit" not in out

    def test_no_truncation_when_limit_covers_all(self, mcp):
        out = _log(mcp, limit=10)
        assert "truncated" not in out

    def test_unknown_repo_rejected(self, mcp):
        with pytest.raises(Exception, match="Unknown repo"):
            _log(mcp, repo="nope")


# ══════════════════════════════════════════════════════════════════════════════
# repo_git_grep
# ══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def grep_repo(tmp_path, monkeypatch):
    """A repo whose matches are far enough apart to produce separate hunks.

    Spacing matters: with the default context git merges nearby hits into one
    block, and these tests are about how blocks are counted. The `regress-1-a.h`
    name is deliberate -- a path containing `-<digits>-` is what makes parsing
    git's `path-N-context` lines by regex ambiguous.
    """
    root = tmp_path / "greppy"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "tester@example.com")
    _git(root, "config", "user.name", "Tester")

    # 4 matches, 40 lines apart, so no two hunks merge at context=5.
    body = []
    for i in range(4):
        body.append(f"NEEDLE occurrence {i}\n")
        body.extend(f"filler {i}.{j}\n" for j in range(40))
    (root / "regress-1-a.h").write_text("".join(body))
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")

    monkeypatch.setattr(
        config,
        "_cache",
        config.Config(repos={"demo": config.Repo(path=root, desc="demo repo")}),
    )
    return root


@pytest.fixture
def grep_mcp(grep_repo):
    server = FastMCP("test")
    repo_git.register(server)
    return server


def _grep(mcp, **args):
    args.setdefault("repo", "demo")
    args.setdefault("pattern", "NEEDLE")
    result = asyncio.run(mcp.call_tool("repo_git_grep", args))
    return result.content[0].text


class TestRepoGitGrep:
    def test_context_on_by_default(self, grep_mcp):
        # The point of the default: a hit arrives with its surroundings, so the
        # caller does not need a follow-up read to see what it found.
        out = _grep(grep_mcp)
        assert "NEEDLE occurrence 0" in out
        assert "filler 0.0" in out  # the line after the first match
        assert "filler 0.4" in out  # ...through the 5th
        assert "filler 0.5" not in out  # but not the 6th

    def test_context_zero_gives_bare_hits(self, grep_mcp):
        out = _grep(grep_mcp, context=0)
        assert "NEEDLE occurrence 0" in out
        assert "filler" not in out
        assert len(out.strip().splitlines()) == 4

    def test_limit_counts_blocks_not_lines(self, grep_mcp):
        # The regression this guards: `limit` used to count output LINES, so
        # with context a limit of 2 returned part of one hunk while the footer
        # claimed 2 matches. It must return two whole blocks.
        out = _grep(grep_mcp, limit=2)
        assert "NEEDLE occurrence 0" in out
        assert "NEEDLE occurrence 1" in out
        assert "NEEDLE occurrence 2" not in out
        assert "truncated — showing first 2 context blocks" in out

    def test_truncation_drops_partial_trailing_block(self, grep_mcp):
        # A truncated result must not end mid-hunk: every block shown is whole.
        out = _grep(grep_mcp, limit=1)
        body = out.split("(truncated")[0]
        assert "NEEDLE occurrence 0" in body
        assert "NEEDLE occurrence 1" not in body
        assert not body.rstrip().endswith("--")

    def test_limit_counts_matches_without_context(self, grep_mcp):
        out = _grep(grep_mcp, context=0, limit=2)
        assert len(out.split("(truncated")[0].strip().splitlines()) == 2
        assert "truncated — showing first 2 matches" in out

    def test_no_truncation_when_limit_covers_all(self, grep_mcp):
        assert "truncated" not in _grep(grep_mcp, limit=10)

    def test_no_matches(self, grep_mcp):
        assert "No matches found" in _grep(grep_mcp, pattern="ABSENTPATTERN")
