"""Reading companion source repos with git: show, grep, find, log, blame, and
worktree selection. Each operation returns the text a caller shows as-is."""

import datetime
import subprocess
from pathlib import Path

from . import config
from . import worktree as worktree_mod
from .paging import paginate_result
from .repos import (
    clear_worktree,
    configured_repo,
    current_branch,
    repo_banner,
    resolve_repo,
    resolve_worktree,
    select_worktree,
    selected_worktree,
)


MAX_READ_LINES = 2000
MAX_GREP_MATCHES = 100
# Bare file:line hits by default; a caller that wants a match's surroundings
# asks for them. A context default invites judging a hit from its few lines
# instead of opening the definition behind it.
DEFAULT_GREP_CONTEXT = 0
MAX_LS_FILES = 500
MAX_LOG_LINES = 2000
MAX_BLAME_LINES = 1000
DEFAULT_BLAME_LINES = 100


def parse_blame_porcelain(text: str) -> tuple[list[tuple[str, int, str]], dict]:
    """Parse `git blame --porcelain` output.

    Returns (lines, commits) where lines is a list of
    (short_hash, final_line_number, content) and commits maps short_hash to
    {"date": "YYYY-MM-DD", "author": str, "summary": str}. Per-commit headers
    appear only the first time a commit is seen in porcelain output, so we
    cache them and reuse for subsequent lines.
    """
    lines: list[tuple[str, int, str]] = []
    commits: dict[str, dict] = {}
    full_details: dict[str, dict] = {}

    cur_hash = None
    cur_final = 0
    pending = {}
    for raw in text.splitlines():
        if raw.startswith("\t"):
            # Content line ends the current block.
            short = cur_hash[:9]
            if cur_hash not in full_details:
                full_details[cur_hash] = pending
                date = ""
                if "author-time" in pending:
                    tz = pending.get("author-tz", "+0000")
                    sign = 1 if tz[0] == "+" else -1
                    offset = datetime.timedelta(
                        hours=int(tz[1:3]), minutes=int(tz[3:5])
                    )
                    dt = datetime.datetime.fromtimestamp(
                        int(pending["author-time"]),
                        tz=datetime.timezone(sign * offset),
                    )
                    date = dt.strftime("%Y-%m-%d")
                commits[short] = {
                    "date": date,
                    "author": pending.get("author", ""),
                    "summary": pending.get("summary", ""),
                }
            lines.append((short, cur_final, raw[1:]))
            pending = {}
            continue

        parts = raw.split(" ", 1)
        if (
            len(parts) == 2
            and len(parts[0]) == 40
            and all(c in "0123456789abcdef" for c in parts[0])
        ):
            # Header line: "<40-hex> <orig> <final> [<count>]".
            hdr = parts[1].split(" ")
            cur_hash = parts[0]
            cur_final = int(hdr[1])
        elif len(parts) == 2:
            pending[parts[0]] = parts[1]

    return lines, commits


def repo_summary() -> str:
    """One-line summary of configured repos for embedding in tool descriptions."""
    cfg = config.load()
    parts = []
    for alias, entry in cfg.repos.items():
        if entry.path.is_dir():
            parts.append(f"{alias} ({entry.desc})" if entry.desc else alias)
    return ", ".join(parts)


def repo_names() -> str:
    """Bare alias list for per-tool descriptions.

    The server instructions carry the full name-and-description list once. A
    tool's own description is read when the repo has already been chosen, so
    repeating the prose five more times buys nothing; the valid names do.
    """
    cfg = config.load()
    return ", ".join(a for a, e in cfg.repos.items() if e.path.is_dir())


def show(
    repo: str,
    path: str | None = None,
    offset: int = 0,
    limit: int = 100,
    ref: str | None = None,
    worktree: str | None = None,
) -> str:
    root = resolve_repo(repo, worktree)
    banner = repo_banner(repo, root)

    if path is None:
        # Commit mode: show commit message + diff
        if not ref:
            raise ValueError("ref is required when path is omitted (commit mode)")
        proc = subprocess.run(
            ["git", "show", "--stat", "--patch", "--end-of-options", ref],
            capture_output=True,
            text=True,
            cwd=root,
        )
        if proc.returncode != 0:
            raise ValueError(f"git show {ref} failed: {proc.stderr.strip()[:500]}")
        lines = proc.stdout.splitlines()
    elif ref:
        proc = subprocess.run(
            ["git", "show", "--end-of-options", f"{ref}:{path}"],
            capture_output=True,
            text=True,
            cwd=root,
        )
        if proc.returncode != 0:
            raise ValueError(
                f"git show {ref}:{path} failed: {proc.stderr.strip()[:500]}"
            )
        lines = proc.stdout.splitlines()
    else:
        root = root.resolve()
        target = (root / path).resolve()
        # Prevent path traversal outside repo root. is_relative_to is a true
        # path-boundary check; a string prefix test would admit siblings
        # that merely share the root's leading characters (e.g. v8-secrets
        # for a v8 root).
        if not target.is_relative_to(root):
            raise ValueError(f"Path escapes repo root: {path}")
        if not target.is_file():
            raise ValueError(f"File not found: {path} (in {root})")
        lines = target.read_text(errors="replace").splitlines()
    return banner + paginate_result(lines, offset, limit, numbered=True)


def grep(
    repo: str,
    pattern: str,
    glob: str | None = None,
    context: int = DEFAULT_GREP_CONTEXT,
    ignore_case: bool = False,
    limit: int = MAX_GREP_MATCHES,
    ref: str | None = None,
    worktree: str | None = None,
) -> str:
    root = resolve_repo(repo, worktree)
    banner = repo_banner(repo, root)
    cmd = ["git", "grep", "-n", "--no-color", "-E"]
    if ignore_case:
        cmd.append("-i")
    if context > 0:
        cmd.append(f"-C{context}")
    # -e keeps a leading-dash pattern from being parsed as an option;
    # --end-of-options does the same for a caller-supplied ref.
    cmd.extend(["-e", pattern])
    if ref:
        cmd.extend(["--end-of-options", ref])
    if glob:
        cmd.extend(["--", glob])

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=root,
    )
    # What `limit` counts depends on whether context was asked for. Without
    # context every output line is a match, so lines and matches are the same
    # number. With it, git emits `context + 1 + context` lines per hit plus a
    # `--` separator between non-adjacent hunks, so counting lines would cut
    # a limit=100 search off at ~9 hits while the footer still claimed 100
    # matches. Count hunks instead: a bare `--` line is git's own separator
    # and cannot be confused with content (every content line is prefixed
    # `path:N:` or `path-N-`), so this needs no path parsing -- which would be
    # ambiguous anyway against V8 paths like `regress-123-foo.h`.
    unit = "matches" if context <= 0 else "context blocks"
    collected: list[str] = []
    hunks = 0
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            # Adjacent hunks are merged by git with no separator, so counting
            # separators alone would undercount; the first hunk has none.
            if context <= 0 or line == "--" or not collected:
                hunks += 1
            collected.append(line)
            if hunks > limit:
                proc.kill()
                break
    finally:
        proc.wait()

    if not collected and proc.returncode == 1:
        return banner + "No matches found."
    if not collected and proc.returncode not in (0, 1, -9):
        stderr = proc.stderr.read() if proc.stderr else ""
        raise ValueError(f"git grep failed: {stderr.strip()[:500]}")

    if hunks > limit:
        # Drop the trailing partial hunk: we stopped mid-stream, so the last
        # block is whatever happened to be read, not a whole one.
        if context > 0:
            while collected and collected[-1] != "--":
                collected.pop()
            if collected and collected[-1] == "--":
                collected.pop()
        else:
            del collected[limit:]
        result = "\n".join(collected)
        result += f"\n(truncated — showing first {limit} {unit})"
    else:
        result = "\n".join(collected)
    return banner + result


def find(
    repo: str,
    glob: str,
    limit: int = MAX_LS_FILES,
    ref: str | None = None,
    worktree: str | None = None,
) -> str:
    root = resolve_repo(repo, worktree)
    banner = repo_banner(repo, root)
    if ref:
        cmd = [
            "git",
            "ls-tree",
            "-r",
            "--name-only",
            "--end-of-options",
            ref,
            "--",
            glob,
        ]
    else:
        cmd = ["git", "ls-files", "--", glob]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=root,
    )
    collected: list[str] = []
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            collected.append(line.rstrip("\n"))
            if len(collected) >= limit + 1:
                proc.kill()
                break
    finally:
        proc.wait()

    if not collected:
        return banner + "No files found."

    if len(collected) > limit:
        result = "\n".join(collected[:limit])
        result += f"\n(truncated — showing first {limit} files)"
    else:
        result = "\n".join(collected)
    return banner + result


def log(
    repo: str,
    path: str | None = None,
    ref: str | None = None,
    limit: int = 20,
    grep: str | None = None,
    author: str | None = None,
    since: str | None = None,
    until: str | None = None,
    worktree: str | None = None,
) -> str:
    root = resolve_repo(repo, worktree)
    banner = repo_banner(repo, root)
    limit = max(1, min(limit, MAX_LOG_LINES))
    cmd = [
        "git",
        "log",
        # Fetch one extra so we can tell the caller output was capped.
        f"-{limit + 1}",
        "--format=%h %as %an  %s",
    ]
    if grep:
        cmd.extend(["--grep", grep, "-i"])
    if author:
        cmd.extend(["--author", author])
    if since:
        cmd.extend(["--since", since])
    if until:
        cmd.extend(["--until", until])
    if ref:
        cmd.extend(["--end-of-options", ref])
    if path:
        cmd.extend(["--", path])

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=root,
    )
    if proc.returncode != 0:
        raise ValueError(f"git log failed: {proc.stderr.strip()[:500]}")
    lines = proc.stdout.strip().split("\n") if proc.stdout.strip() else []
    if not lines:
        return banner + "No commits found."
    if len(lines) > limit:
        result = "\n".join(lines[:limit])
        result += f"\n(truncated — showing first {limit} commits)"
    else:
        result = "\n".join(lines)
    return banner + result


def blame(
    repo: str,
    path: str,
    start: int = 1,
    limit: int = DEFAULT_BLAME_LINES,
    ref: str | None = None,
    worktree: str | None = None,
) -> str:
    root = resolve_repo(repo, worktree)
    banner = repo_banner(repo, root)
    if start < 1:
        raise ValueError("start must be >= 1")
    limit = max(1, min(limit, MAX_BLAME_LINES))

    # Blame only the requested window. Request one extra line so we can tell
    # the caller whether more lines follow without blaming the whole file.
    # git clamps the end of the range at EOF, so overshooting is harmless.
    cmd = ["git", "blame", "--porcelain", f"-L{start},+{limit + 1}"]
    if ref:
        # git blame does not honor a `--` path separator after
        # --end-of-options (unlike git log/grep), so pass the path as a
        # bare positional. --end-of-options still neutralizes a leading-dash
        # ref or path, keeping option injection impossible.
        cmd.extend(["--end-of-options", ref, path])
    else:
        cmd.extend(["--", path])

    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=root)
    if proc.returncode != 0:
        raise ValueError(f"git blame failed: {proc.stderr.strip()[:500]}")

    lines, commits = parse_blame_porcelain(proc.stdout)
    if not lines:
        return banner + "No lines to blame (file empty or range out of range)."

    more = len(lines) > limit
    shown = lines[:limit]
    width = max(len(str(n)) for _, n, _ in shown)
    body = "\n".join(f"{n:>{width}} {h} {content}" for h, n, content in shown)

    seen: list[str] = []
    for h, _, _ in shown:
        if h not in seen:
            seen.append(h)
    legend = "\n".join(
        f"  {h}  {commits[h]['date']}  {commits[h]['author']}  {commits[h]['summary']}"
        for h in seen
    )

    out = banner + body + "\n\nCommits:\n" + legend
    if more:
        next_start = start + len(shown)
        out += (
            f"\n\n(showing lines {start}-{start + len(shown) - 1};"
            f" more follow, continue at start={next_start})"
        )
    return out


def worktree_select(
    name: str | None = None,
    repo: str = "v8",
) -> str:
    if name is None:
        previous = clear_worktree(repo)
        root = configured_repo(repo)
        note = f" (was {previous.name})" if previous is not None else ""
        return (
            f"Using the main {repo} checkout{note}: {root}\n"
            f"branch {current_branch(root)}"
        )

    path = resolve_worktree(repo, name)
    select_worktree(repo, path)

    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=path,
        capture_output=True,
        text=True,
    )
    n_dirty = len([ln for ln in dirty.stdout.splitlines() if ln.strip()])
    clean = "clean" if n_dirty == 0 else f"{n_dirty} uncommitted file(s)"
    return (
        f"repo_git_* now read {repo} worktree {path.name}: {path}\n"
        f"branch {current_branch(path)} | {clean}"
    )


def worktree_list(
    repo: str = "v8",
) -> str:
    root = configured_repo(repo)
    try:
        worktrees = worktree_mod.list_worktrees(root)
    except (subprocess.CalledProcessError, OSError) as exc:
        raise ValueError(f"Cannot list worktrees for {root}: {exc}") from exc
    if not worktrees:
        return "No worktrees found."

    active = selected_worktree(repo)
    lines = [f"{'':2} {'path':<50} {'branch':<32} head"]
    lines.append("-" * len(lines[0]))
    for wt in worktrees:
        mark = "*" if active is not None and Path(wt.path) == active else " "
        lines.append(f"{mark:2} {wt.path:<50} {wt.branch:<32} {wt.head}")
    if active is None:
        lines.append("\nNo worktree selected; repo_git_* read the main checkout.")
    else:
        lines.append(f"\nSelected (*): repo_git_* read {active}")
    return "\n".join(lines)
