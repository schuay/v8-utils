"""Git worktree management for V8 with gclient dependency symlinking."""

import json
import subprocess
from pathlib import Path


def _run(cmd: list[str], *, cwd: Path | None = None) -> str:
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True)
    return r.stdout.strip()


def _find_gclient_root(repo: Path) -> Path:
    """Walk up from repo to find the directory containing .gclient."""
    d = repo.parent
    while d != d.parent:
        if (d / ".gclient").is_file():
            return d
        d = d.parent
    raise ValueError(f"no .gclient found above {repo}")


def _find_main_worktree(repo: Path) -> Path:
    """Find the main (non-linked) worktree — the one with a real .git dir."""
    lines = _run(["git", "worktree", "list", "--porcelain"], cwd=repo)
    for line in lines.splitlines():
        if line.startswith("worktree "):
            path = Path(line.split(" ", 1)[1])
            if (path / ".git").is_dir():
                return path
    raise ValueError("could not find main worktree")


def _gclient_dep_paths(gclient_root: Path, solution: str) -> list[str]:
    """Query gclient revinfo for all dependency paths under the solution."""
    r = subprocess.run(
        ["gclient", "revinfo", "--output-json=/dev/stdout"],
        cwd=gclient_root,
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(r.stdout)
    prefix = solution + "/"

    paths: set[str] = set()
    for key in data:
        path = key.split(":")[0]  # strip CIPD/hash suffixes
        if path.startswith(prefix):
            path = path[len(prefix) :]
        elif path == solution:
            continue
        else:
            continue
        if path:
            paths.add(path)
    return sorted(paths)


def _symlink_paths(main: Path, gclient_root: Path) -> list[str]:
    """Compute the minimal set of paths that need symlinking into a worktree."""
    solution = main.relative_to(gclient_root).as_posix()
    dep_paths = _gclient_dep_paths(gclient_root, solution)

    # Keep only paths not tracked by git.
    untracked = []
    for p in dep_paths:
        r = subprocess.run(
            ["git", "ls-tree", "--name-only", "HEAD", p],
            cwd=main,
            capture_output=True,
            text=True,
        )
        if not r.stdout.strip():
            untracked.append(p)

    # Minimize: skip paths whose ancestor is already in the set.
    untracked_set = set(untracked)
    minimal = []
    for p in untracked:
        parts = p.split("/")
        if not any("/".join(parts[:i]) in untracked_set for i in range(1, len(parts))):
            minimal.append(p)
    return minimal


def _validate_name(name: str) -> None:
    """Reject names that could escape the gclient root."""
    if "/" in name or name in (".", "..") or name.startswith("."):
        raise ValueError(
            f"invalid worktree name {name!r}: must be a plain directory name"
        )


_DEFAULT_BUILDS = ["x64.optdebug", "x64.release"]


def _setup_builds(wt_path: Path, builds: list[str]) -> list[str]:
    """Run gm.py gn_args for each build config. Returns status lines."""
    gm = wt_path / "tools" / "dev" / "gm.py"
    if not gm.exists():
        return [f"(gm.py not found, skipping build setup)"]
    results = []
    for build in builds:
        try:
            _run(["python3", str(gm), f"{build}.gn_args"], cwd=wt_path)
            results.append(f"  out/{build.replace('.', '.')}: ok")
        except subprocess.CalledProcessError as e:
            results.append(
                f"  out/{build.replace('.', '.')}: FAILED ({e.stderr.strip()[:80]})"
            )
    return results


def create(
    repo: Path,
    name: str,
    branch: str | None = None,
    upstream: str = "main",
) -> dict:
    """Create a worktree as a sibling of the main checkout, symlink gclient deps.

    upstream: base branch/ref for the new branch (default "main").
    Returns {path, builds} with the worktree path and build setup results.
    """
    _validate_name(name)
    main = _find_main_worktree(repo)
    gclient_root = _find_gclient_root(main)
    wt_path = gclient_root / name

    if wt_path.exists():
        raise ValueError(f"path already exists: {wt_path}")

    # Create git worktree.
    cmd = ["git", "worktree", "add"]
    if branch and _branch_exists(main, branch):
        cmd += [str(wt_path), branch]
    else:
        # -b creates a new branch; use explicit name or default to worktree name.
        cmd += ["-b", branch or name, str(wt_path), upstream]
    _run(cmd, cwd=main)

    # Symlink gclient-managed deps.
    paths = _symlink_paths(main, gclient_root)
    for dep in paths:
        src = main / dep
        dst = wt_path / dep
        if not src.exists() or dst.exists() or dst.is_symlink():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        rel = Path(src).resolve().relative_to(dst.parent.resolve(), walk_up=True)
        dst.symlink_to(rel)

    # Set up default build directories.
    build_results = _setup_builds(wt_path, _DEFAULT_BUILDS)

    return {"path": wt_path, "builds": build_results}


def _remove_external_symlinks(wt_path: Path) -> None:
    """Remove all symlinks under wt_path that point outside of it."""
    resolved_root = str(wt_path.resolve())
    # find -type l is fast: it uses d_type from readdir to skip non-symlinks
    # without stat'ing millions of build artifacts in out/.
    result = subprocess.run(
        ["find", str(wt_path), "-type", "l"],
        capture_output=True,
        text=True,
    )
    for line in result.stdout.splitlines():
        link = Path(line)
        if not str(link.resolve()).startswith(resolved_root):
            link.unlink()


def remove(repo: Path, name: str, force: bool = False) -> None:
    """Remove a worktree: clean up symlinks then git worktree remove."""
    _validate_name(name)
    main = _find_main_worktree(repo)
    gclient_root = _find_gclient_root(main)
    wt_path = gclient_root / name

    if not wt_path.exists():
        raise ValueError(f"worktree not found: {wt_path}")

    # Remove symlinks pointing outside the worktree so git doesn't see
    # untracked content. This is independent of gclient — robust against
    # DEPS changes since creation.
    _remove_external_symlinks(wt_path)

    cmd = ["git", "worktree", "remove", str(wt_path)]
    if force:
        cmd.append("--force")
    _run(cmd, cwd=main)


def list_worktrees(repo: Path) -> list[dict]:
    """List all worktrees. Returns list of {path, branch, head}."""
    lines = _run(["git", "worktree", "list", "--porcelain"], cwd=repo)
    worktrees = []
    current: dict = {}
    for line in lines.splitlines():
        if line.startswith("worktree "):
            if current:
                worktrees.append(current)
            current = {"path": line.split(" ", 1)[1]}
        elif line.startswith("HEAD "):
            current["head"] = line.split(" ", 1)[1][:12]
        elif line.startswith("branch "):
            current["branch"] = line.split(" ", 1)[1].removeprefix("refs/heads/")
        elif line == "detached":
            current["branch"] = "(detached)"
    if current:
        worktrees.append(current)
    return worktrees


def _branch_exists(repo: Path, branch: str) -> bool:
    r = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/heads/{branch}"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    return r.returncode == 0
