"""Git worktree management for V8 with gclient dependency symlinking."""

import json
import re
import shutil
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


_DEFAULT_BUILDS = ["x64.optdebug", "x64.debug", "x64.release"]


def _force_symbol_level(args_gn: Path) -> None:
    """Rewrite or append `symbol_level = 2` in args.gn."""
    text = args_gn.read_text()
    pattern = re.compile(r"^symbol_level\s*=.*$", re.MULTILINE)
    if pattern.search(text):
        new_text = pattern.sub("symbol_level = 2", text)
    else:
        new_text = text if text.endswith("\n") else text + "\n"
        new_text += "symbol_level = 2\n"
    if new_text != text:
        args_gn.write_text(new_text)


def _force_remoteexec(args_gn: Path) -> None:
    """Force remote build execution in args.gn.

    gm.py only writes `use_remoteexec = true` when it detects reclient at gn-gen
    time (via .gclient custom_vars), which is host-dependent. This pins it on so
    worktree builds always run remotely regardless of that detection. Requires
    the host to have reclient configs (buildtools/reclient_cfgs) and RBE auth.
    """
    text = args_gn.read_text()
    new_text = re.sub(
        r"^use_(?:remoteexec|goma)\s*=.*$",
        "use_remoteexec = true",
        text,
        flags=re.MULTILINE,
    )
    if "use_remoteexec = true" not in new_text:
        new_text = new_text if new_text.endswith("\n") else new_text + "\n"
        new_text += "use_remoteexec = true\n"
    if not re.search(r"^reclient_cfg_dir\s*=", new_text, re.MULTILINE):
        new_text += 'reclient_cfg_dir = "../../buildtools/reclient_cfgs/linux"\n'
    if new_text != text:
        args_gn.write_text(new_text)


def _setup_builds(wt_path: Path, builds: list[str]) -> list[str]:
    """Prepare each build dir: write args.gn (gm.py), force symbol_level=2 and
    remote execution, run gn gen."""
    gm = wt_path / "tools" / "dev" / "gm.py"
    if not gm.exists():
        return ["(gm.py not found, skipping build setup)"]
    results = []
    for build in builds:
        label = f"  out/{build}"
        try:
            _run(["python3", str(gm), f"{build}.gn_args"], cwd=wt_path)
            args_gn = wt_path / "out" / build / "args.gn"
            _force_symbol_level(args_gn)
            _force_remoteexec(args_gn)
            _run(["gn", "gen", f"out/{build}"], cwd=wt_path)
            results.append(f"{label}: ok")
        except subprocess.CalledProcessError as e:
            err = (e.stderr or e.stdout or "").strip()[:80]
            results.append(f"{label}: FAILED ({err})")
    return results


def _force_cleanup(main: Path, wt_path: Path, branch: str) -> None:
    """Tear down any leftover state for a worktree so a re-create starts clean.

    Handles each leftover independently and best-effort, so it is robust to the
    partial states that make plain `create` brittle: a registered worktree, a
    stray directory git no longer tracks, a stale registration whose dir is
    gone, and a dangling same-named branch. Safe to call when nothing is left.
    """
    registered = any(
        Path(wt["path"]).resolve() == wt_path.resolve() for wt in list_worktrees(main)
    )
    if registered:
        _remove_external_symlinks(wt_path)
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(wt_path)],
            cwd=main,
            capture_output=True,
            text=True,
        )
    if wt_path.exists():
        # A stray directory not tracked by git (or one remove left behind).
        _remove_external_symlinks(wt_path)
        shutil.rmtree(wt_path, ignore_errors=True)
    # Drop any registration whose directory is already gone.
    subprocess.run(
        ["git", "worktree", "prune"], cwd=main, capture_output=True, text=True
    )
    # Delete a dangling same-named branch, unless protected or in use elsewhere.
    if branch not in ("main", "master") and _branch_exists(main, branch):
        in_use = any(
            wt.get("branch") == branch
            and Path(wt["path"]).resolve() != wt_path.resolve()
            for wt in list_worktrees(main)
        )
        if not in_use:
            subprocess.run(
                ["git", "branch", "-D", branch],
                cwd=main,
                capture_output=True,
                text=True,
            )


def create(
    repo: Path,
    name: str,
    branch: str | None = None,
    upstream: str = "main",
    force: bool = False,
) -> dict:
    """Create a worktree as a sibling of the main checkout, symlink gclient deps.

    upstream: base branch/ref for the new branch (default "main").
    force: tear down any leftover worktree dir and dangling same-named branch
    first, so re-creating is idempotent (a fresh attempt). Without it, an
    existing path raises.
    Returns {path, builds} with the worktree path and build setup results.
    """
    _validate_name(name)
    main = _find_main_worktree(repo)
    gclient_root = _find_gclient_root(main)
    wt_path = gclient_root / name

    if force:
        _force_cleanup(main, wt_path, branch or name)
    elif wt_path.exists():
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
    _create_symlinks(main, wt_path, gclient_root)

    # Set up default build directories.
    build_results = _setup_builds(wt_path, _DEFAULT_BUILDS)

    return {"path": wt_path, "builds": build_results}


def _create_symlinks(main: Path, wt_path: Path, gclient_root: Path) -> list[str]:
    """Create symlinks for the current set of gclient deps. Skips existing links.

    Returns the list of dep paths that were linked.
    """
    linked = []
    for dep in _symlink_paths(main, gclient_root):
        src = main / dep
        dst = wt_path / dep
        if not src.exists() or dst.exists() or dst.is_symlink():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        rel = Path(src).resolve().relative_to(dst.parent.resolve(), walk_up=True)
        dst.symlink_to(rel)
        linked.append(dep)
    return linked


def refresh(repo: Path, name: str) -> dict:
    """Rebuild a worktree's gclient dep symlinks against the current DEPS.

    The symlink set is a snapshot taken at create time. After rebasing the
    worktree onto a different main, the checked-out DEPS no longer matches it:
    new deps have no link, removed deps leave stale links, moved deps do both.
    This removes all external symlinks and recreates them from the current DEPS.

    Targets resolve into the main checkout, so run `gclient sync` there first
    if the rebase pulled in deps main has not synced yet.

    Returns {path, linked} with the worktree path and the deps that were linked.
    """
    _validate_name(name)
    main = _find_main_worktree(repo)
    gclient_root = _find_gclient_root(main)
    wt_path = gclient_root / name

    if not wt_path.exists():
        raise ValueError(f"worktree not found: {wt_path}")

    # Remove first: the create loop skips existing links, so stale ones must go
    # before recreation. _remove_external_symlinks also clears dangling links.
    _remove_external_symlinks(wt_path)
    linked = _create_symlinks(main, wt_path, gclient_root)
    return {"path": wt_path, "linked": linked}


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


def remove(
    repo: Path,
    name: str,
    force: bool = False,
    remove_branch: bool = False,
) -> dict:
    """Remove a worktree: clean up symlinks then git worktree remove.

    If remove_branch is true, also delete the underlying git branch.
    Skipped for detached HEAD, `main`/`master`, or branches checked out
    in another worktree.

    Returns {branch, branch_removed, note} describing the branch outcome.
    """
    _validate_name(name)
    main = _find_main_worktree(repo)
    gclient_root = _find_gclient_root(main)
    wt_path = gclient_root / name

    if not wt_path.exists():
        raise ValueError(f"worktree not found: {wt_path}")

    worktrees = list_worktrees(main)
    branch: str | None = None
    for wt in worktrees:
        if Path(wt["path"]).resolve() == wt_path.resolve():
            b = wt.get("branch")
            if b and b != "(detached)":
                branch = b
            break

    # Remove symlinks pointing outside the worktree so git doesn't see
    # untracked content. This is independent of gclient — robust against
    # DEPS changes since creation.
    _remove_external_symlinks(wt_path)

    cmd = ["git", "worktree", "remove", str(wt_path)]
    if force:
        cmd.append("--force")
    _run(cmd, cwd=main)

    result = {"branch": branch, "branch_removed": False, "note": None}
    if not remove_branch:
        return result
    if branch is None:
        result["note"] = "no branch to delete (detached HEAD)"
        return result
    if branch in ("main", "master"):
        result["note"] = f"refusing to delete protected branch {branch!r}"
        return result
    for wt in worktrees:
        if Path(wt["path"]).resolve() == wt_path.resolve():
            continue
        if wt.get("branch") == branch:
            result["note"] = f"branch {branch!r} also checked out in {wt['path']}"
            return result

    _run(["git", "branch", "-D", branch], cwd=main)
    result["branch_removed"] = True
    return result


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
