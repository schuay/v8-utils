"""Which checkout a repo name refers to: the configured path, a named
worktree, or the worktree selected for the session."""

import subprocess
from pathlib import Path

from . import config
from . import worktree as worktree_mod


# Worktree selected for a repo via repo_git_worktree_select, keyed by repo name.
# Process-global, but each Claude Code session spawns its own server, so this is
# effectively session state. A session's subagents do share it -- that is what
# the per-call `worktree` parameter is for.
#
# Reach it only through the accessors below. Importing the dict itself binds the
# object at import time, so a caller that rebinds this name (a test isolating
# state, say) would leave readers and writers on two different dicts.
_active_worktree: dict[str, Path] = {}


def selected_worktree(repo: str) -> Path | None:
    """Path selected for `repo`, or None when the main checkout is in use."""
    return _active_worktree.get(repo)


def select_worktree(repo: str, path: Path) -> None:
    _active_worktree[repo] = path


def clear_worktree(repo: str) -> Path | None:
    """Drop any selection for `repo`, returning what was selected."""
    return _active_worktree.pop(repo, None)


def worktree_map(root: Path) -> dict[str, Path]:
    """Map every worktree of `root`'s repo to its path, by dir name and branch.

    git reports absolute paths for all worktrees from any member of the set, so
    no configuration beyond the repo entry point is needed. Directory names are
    added last: they win a collision with an unrelated worktree's branch name.
    """
    try:
        worktrees = worktree_mod.list_worktrees(root)
    except (subprocess.CalledProcessError, OSError) as exc:
        raise ValueError(f"Cannot list worktrees for {root}: {exc}") from exc
    by_branch: dict[str, Path] = {}
    by_dir: dict[str, Path] = {}
    for wt in worktrees:
        path = Path(wt.path)
        branch = wt.branch
        if branch and branch != "(detached)":
            by_branch[branch] = path
        by_dir[path.name] = path
    return {**by_branch, **by_dir}


def resolve_worktree(repo: str, name: str) -> Path:
    """Resolve a worktree name (directory or branch) to its absolute path."""
    root = configured_repo(repo)
    candidates = worktree_map(root)
    path = candidates.get(name)
    if path is None:
        valid = ", ".join(sorted(candidates))
        raise ValueError(
            f"Unknown worktree {name!r} in repo {repo!r}. Available: {valid}"
        )
    if not path.is_dir():
        raise ValueError(f"Worktree {name!r} path does not exist: {path}")
    return path


def configured_repos() -> dict[str, config.Repo]:
    """Every repo in the config, by name, whether or not its path exists."""
    return config.load().repos


def configured_repo(repo: str) -> Path:
    """Resolve a repo name to its configured path, ignoring worktree selection."""
    cfg = config.load()
    entry = cfg.repos.get(repo)
    if entry is None:
        valid = ", ".join(sorted(cfg.repos))
        raise ValueError(f"Unknown repo {repo!r}. Configured repos: {valid}")
    if not entry.path.is_dir():
        raise ValueError(f"Repo {repo!r} path does not exist: {entry.path}")
    return entry.path


def resolve_repo(repo: str, worktree: str | None = None) -> Path:
    """Resolve a repo name to the path git commands should run in.

    Precedence: the explicit `worktree` argument, then the worktree selected for
    this repo via repo_git_worktree_select, then the configured path.
    """
    if worktree is not None:
        return resolve_worktree(repo, worktree)
    selected = selected_worktree(repo)
    if selected is not None:
        if not selected.is_dir():
            # Removed while selected: say so rather than silently reading main.
            raise ValueError(
                f"Selected worktree {selected.name!r} no longer exists at {selected}. "
                f"Call repo_git_worktree_select with no name to return to the "
                f"main checkout."
            )
        return selected
    return configured_repo(repo)


def current_branch(root: Path) -> str:
    """Branch checked out at `root`, or "detached at <short hash>"."""
    r = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    branch = r.stdout.strip()
    if r.returncode != 0 or not branch:
        return "?"
    if branch != "HEAD":
        return branch
    # Detached: --abbrev-ref reports the literal string "HEAD", which says
    # nothing about where the worktree actually is.
    r = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    short = r.stdout.strip()
    return f"detached at {short}" if r.returncode == 0 and short else "detached"


def repo_banner(repo: str, root: Path) -> str:
    """Result prefix naming the worktree in use, empty for the main checkout.

    Worktree selection is sticky and otherwise invisible; this puts it in the
    transcript so drift is noticeable at the point it would mislead.
    """
    try:
        # Resolve both sides: the configured path is whatever the user wrote,
        # while worktree paths come from git already canonical, so a symlinked
        # or non-normalized config path would otherwise never compare equal.
        if root.resolve() == configured_repo(repo).resolve():
            return ""
    except (ValueError, OSError):
        return ""
    return f"[{repo} @ {root.name} | branch {current_branch(root)}]\n"
