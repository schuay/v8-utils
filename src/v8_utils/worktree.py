"""Git worktree management for V8 with gclient dependency symlinking."""

from __future__ import annotations

import ctypes
import gzip
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]


@dataclass(frozen=True)
class WorktreeInfo:
    """One entry of `git worktree list`. branch is "(detached)" for a detached
    HEAD and "" for a bare entry."""

    path: str
    branch: str = ""
    head: str = ""


@dataclass(frozen=True)
class Created:
    path: Path
    #: One line per build directory prepared, as gm.py reported it.
    builds: list[str]


@dataclass(frozen=True)
class Refreshed:
    path: Path
    #: Dependency paths newly symlinked; empty when all were present.
    linked: list[str]


@dataclass(frozen=True)
class Removed:
    #: The worktree's branch, None when it was detached.
    branch: str | None
    branch_removed: bool
    #: Why a requested branch removal did not happen, else None.
    note: str | None


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
            check=False,
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


# Matches gm.py, which refuses x64 builds on arm64 (Apple silicon) hosts.
BUILD_ARCH = "arm64" if platform.machine() == "arm64" else "x64"
_DEFAULT_BUILDS = [f"{BUILD_ARCH}.{mode}" for mode in ("optdebug", "debug", "release")]


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
    if new_text != text:
        args_gn.write_text(new_text)


# <sys/clonefile.h> flag: clone symlinks themselves rather than following them.
_CLONE_NOFOLLOW = 0x0001
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
_GZIP_MAGIC = b"\x1f\x8b"


class _CowUnavailable(Exception):
    """Raised when APFS build cloning cannot proceed and gm.py should be used."""


def _supports_clonefile(src_file: Path, dst_dir: Path) -> bool:
    """Return True iff macOS clonefile(2) succeeds from src_file into dst_dir.

    BSD `cp -c` silently falls back to a full physical copy across volumes or on
    non-APFS filesystems. Probing a single file with clonefile(2) directly
    guarantees we never trigger a multi-GB copy when COW is unavailable.
    """
    if sys.platform != "darwin":
        return False
    try:
        if src_file.stat().st_dev != dst_dir.stat().st_dev:
            return False
        libc = ctypes.CDLL(None, use_errno=True)
        clonefile = libc.clonefile
        clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
        clonefile.restype = ctypes.c_int
    except (OSError, AttributeError):
        return False

    probe = dst_dir / f".cow-probe-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    try:
        rc = clonefile(os.fsencode(src_file), os.fsencode(probe), _CLONE_NOFOLLOW)
        return rc == 0
    finally:
        probe.unlink(missing_ok=True)


def _is_siso_locked(build_dir: Path) -> bool:
    """Return True if a Siso build currently holds an exclusive lock on build_dir."""
    lock = build_dir / ".siso_lock"
    if fcntl is None or not lock.is_file():
        return False
    try:
        fd = os.open(lock, os.O_RDONLY)
    except OSError:
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _is_warm_build(build_dir: Path) -> bool:
    """Return True if build_dir has compiled artifacts worth COW-cloning.

    A directory that was only `gn gen`'d (or `gn clean`'d) already has args.gn,
    build.ninja, and an obj/ tree of sub-ninja files, but no build log
    (.siso_fs_state or .ninja_log). Require a build log so unbuilt dirs take
    the normal gm.py path (which applies symbol_level=2 and remoteexec).
    """
    return (
        build_dir.is_dir()
        and (build_dir / "args.gn").is_file()
        and (build_dir / "build.ninja").is_file()
        and (
            (build_dir / ".siso_fs_state").is_file()
            or (build_dir / ".ninja_log").is_file()
        )
        and not _is_siso_locked(build_dir)
    )


def _decompress_siso_state(raw: bytes) -> tuple[bytes, str]:
    """Decompress a Siso .siso_fs_state payload, returning (bytes, codec)."""
    if raw.startswith(_GZIP_MAGIC):
        return gzip.decompress(raw), "gzip"
    if raw.startswith(_ZSTD_MAGIC):
        try:
            from compression import zstd  # Python 3.14+ stdlib

            return zstd.decompress(raw), "zstd"
        except ImportError:
            pass
        try:
            import zstandard  # type: ignore[import-not-found]

            return zstandard.ZstdDecompressor().decompress(raw), "zstd"
        except ImportError:
            pass
        zstd_bin = shutil.which("zstd")
        if zstd_bin:
            r = subprocess.run(
                [zstd_bin, "-d", "-c", "-q"],
                input=raw,
                capture_output=True,
                check=True,
            )
            return r.stdout, "zstd"
        raise _CowUnavailable("zstd decoder unavailable for .siso_fs_state")
    raise _CowUnavailable("unrecognized .siso_fs_state compression format")


def _compress_siso_state(payload: bytes, codec: str) -> bytes:
    """Recompress a Siso .siso_fs_state payload with the original codec."""
    if codec == "gzip":
        return gzip.compress(payload)
    if codec == "zstd":
        try:
            from compression import zstd  # Python 3.14+ stdlib

            return zstd.compress(payload)
        except ImportError:
            pass
        try:
            import zstandard  # type: ignore[import-not-found]

            return zstandard.ZstdCompressor().compress(payload)
        except ImportError:
            pass
        zstd_bin = shutil.which("zstd")
        if zstd_bin:
            r = subprocess.run(
                [zstd_bin, "-c", "-q"],
                input=payload,
                capture_output=True,
                check=True,
            )
            return r.stdout
        raise _CowUnavailable("zstd encoder unavailable for .siso_fs_state")
    raise _CowUnavailable(f"unsupported codec: {codec!r}")


def _read_varint(buf: bytes, offset: int) -> tuple[int, int]:
    """Read an unsigned protobuf varint starting at offset."""
    value = 0
    shift = 0
    end = len(buf)
    while offset < end:
        byte = buf[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return value, offset
        shift += 7
        if shift >= 70:
            raise ValueError("malformed protobuf varint")
    raise ValueError("truncated protobuf varint")


def _encode_varint(value: int) -> bytes:
    """Encode a non-negative integer as a protobuf varint."""
    if value < 0:
        raise ValueError("varint must be non-negative")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _skip_pb_field(buf: bytes, offset: int, wire_type: int) -> int:
    """Advance offset past one protobuf field payload of wire_type."""
    end = len(buf)
    if wire_type == 0:
        _, offset = _read_varint(buf, offset)
    elif wire_type == 1:
        offset += 8
    elif wire_type == 2:
        length, offset = _read_varint(buf, offset)
        offset += length
    elif wire_type == 5:
        offset += 4
    else:
        raise ValueError(f"unsupported protobuf wire type {wire_type}")
    if offset > end:
        raise ValueError("truncated protobuf field")
    return offset


def _under_any_prefix(rel: bytes, prefixes: tuple[bytes, ...]) -> bool:
    return any(rel == p or rel.startswith(p + b"/") for p in prefixes)


def _rewrite_siso_entry(
    entry: bytes,
    *,
    old_root: bytes,
    old_prefix: bytes,
    new_root: bytes,
    new_prefix: bytes,
    symlink_prefixes: tuple[bytes, ...],
    drop_rel_paths: set[bytes],
) -> bytes | None:
    """Rewrite `Entry.name` (field 2) inside a Siso hashfs Entry message.

    Returns:
    - None if the entry belongs to a path in `drop_rel_paths` (so Siso re-stats
      and re-hashes it unconditionally).
    - The original `entry` object unchanged if no rewrite was needed (e.g.
      entries under gclient-symlinked deps or system SDK paths).
    - Re-encoded entry bytes with the rewritten `name` otherwise.
    """
    offset = 0
    end = len(entry)
    out = bytearray()
    changed = False

    while offset < end:
        field_start = offset
        tag, offset = _read_varint(entry, offset)
        field_num, wire_type = tag >> 3, tag & 7

        if field_num == 2 and wire_type == 2:
            length, offset = _read_varint(entry, offset)
            value_end = offset + length
            if value_end > end:
                raise ValueError("truncated Entry.name")
            name = entry[offset:value_end]
            offset = value_end

            if name == old_root:
                new_name = new_root
            elif name.startswith(old_prefix):
                rel = name[len(old_prefix) :]
                if rel in drop_rel_paths:
                    return None
                if _under_any_prefix(rel, symlink_prefixes):
                    out.extend(entry[field_start:offset])
                    continue
                new_name = new_prefix + rel
            else:
                out.extend(entry[field_start:offset])
                continue

            out.extend(_encode_varint(tag))
            out.extend(_encode_varint(len(new_name)))
            out.extend(new_name)
            changed = True
        else:
            offset = _skip_pb_field(entry, offset, wire_type)
            out.extend(entry[field_start:offset])

    return bytes(out) if changed else entry


def _rewrite_siso_fs_state(
    fs_state_path: Path,
    *,
    old_root: Path,
    new_root: Path,
    symlinked_deps: Sequence[str],
    drop_paths: Collection[str],
) -> None:
    """Relocate worktree-local entries in Siso's .siso_fs_state protobuf.

    Siso stores absolute paths in `State.entries[i].name` (field 1 -> Entry,
    field 2 -> name) and resolves symlinks before recording them. After cloning
    `main/out/<build>` into a sibling worktree:
    - Entries under gclient-symlinked deps (build/, third_party/*, ...) still
      resolve into `main`, so their paths stay under `old_root`.
    - Worktree-local entries (out/<build>/*, src/*, ...) are rewritten from
      `old_root` to `new_root` so Siso hits its build log instead of
      re-materializing every object file and breaking COW.
    - Entries in `drop_paths` (dirty in main or differing between HEADs) are
      omitted so Siso always re-stats and re-hashes them.
    """
    raw = fs_state_path.read_bytes()
    dec, codec = _decompress_siso_state(raw)

    old_root_bytes = os.fsencode(old_root.resolve())
    new_root_bytes = os.fsencode(new_root.resolve())
    old_prefix = old_root_bytes + b"/"
    new_prefix = new_root_bytes + b"/"
    symlink_prefixes = tuple(
        d.strip("/").encode("utf-8") for d in symlinked_deps if d.strip("/")
    )
    drop_rel_paths = {p.strip("/").encode("utf-8") for p in drop_paths if p.strip("/")}

    offset = 0
    end = len(dec)
    out = bytearray()

    while offset < end:
        field_start = offset
        tag, offset = _read_varint(dec, offset)
        field_num, wire_type = tag >> 3, tag & 7

        if field_num == 1 and wire_type == 2:
            length, offset = _read_varint(dec, offset)
            value_end = offset + length
            if value_end > end:
                raise ValueError("truncated State.entries element")
            entry = dec[offset:value_end]
            offset = value_end

            rewritten = _rewrite_siso_entry(
                entry,
                old_root=old_root_bytes,
                old_prefix=old_prefix,
                new_root=new_root_bytes,
                new_prefix=new_prefix,
                symlink_prefixes=symlink_prefixes,
                drop_rel_paths=drop_rel_paths,
            )
            if rewritten is None:
                continue
            if rewritten is entry:
                out.extend(dec[field_start:offset])
            else:
                out.extend(_encode_varint(tag))
                out.extend(_encode_varint(len(rewritten)))
                out.extend(rewritten)
        else:
            offset = _skip_pb_field(dec, offset, wire_type)
            out.extend(dec[field_start:offset])

    recompressed = _compress_siso_state(bytes(out), codec)
    tmp = fs_state_path.with_name(f".siso_fs_state.tmp-{os.getpid()}")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            f = os.fdopen(fd, "wb")
        except Exception:
            os.close(fd)
            raise
        with f:
            f.write(recompressed)
        os.replace(tmp, fs_state_path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _paths_differing_from_main(main: Path, wt_path: Path) -> set[str]:
    """Return tracked paths whose content in wt_path may differ from main.

    Any tracked file in wt_path NOT in this set has the exact same blob SHA as
    main's HEAD and is clean in main's working tree, so copying main's mtime to
    wt_path is guaranteed sound.
    """
    differing: set[str] = set()
    main_head = _run(["git", "rev-parse", "HEAD"], cwd=main)
    wt_head = _run(["git", "rev-parse", "HEAD"], cwd=wt_path)
    if main_head != wt_head:
        r = subprocess.run(
            ["git", "diff", "--no-renames", "--name-only", "-z", main_head, wt_head],
            cwd=main,
            capture_output=True,
            text=True,
            check=True,
        )
        differing.update(p for p in r.stdout.split("\0") if p)

    # Tracked files modified, staged, or deleted in main's working tree.
    r = subprocess.run(
        ["git", "diff", "--no-renames", "--name-only", "-z", "HEAD"],
        cwd=main,
        capture_output=True,
        text=True,
        check=True,
    )
    differing.update(p for p in r.stdout.split("\0") if p)
    return differing


def _sync_mtimes_from_main(
    main: Path, wt_path: Path, skip_paths: Collection[str]
) -> None:
    """Copy mtimes from main for tracked files identical to main's working tree.

    `git worktree add` stamps every checked-out file with the current time,
    which is newer than cloned build outputs and would trigger a full rebuild.
    Restoring main's nanosecond timestamps keeps unchanged translation units
    up to date; refreshing the worktree index keeps subsequent `git status`
    fast.
    """
    r = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=wt_path,
        capture_output=True,
        text=True,
        check=True,
    )
    skip = set(skip_paths)
    for rel in r.stdout.split("\0"):
        if not rel or rel in skip:
            continue
        src = main / rel
        dst = wt_path / rel
        try:
            src_st = os.lstat(src)
            dst_st = os.lstat(dst)
        except OSError:
            continue
        if stat.S_ISREG(src_st.st_mode) and stat.S_ISREG(dst_st.st_mode):
            try:
                os.utime(dst, ns=(src_st.st_atime_ns, src_st.st_mtime_ns))
            except OSError:
                continue

    subprocess.run(
        ["git", "update-index", "-q", "--refresh"],
        cwd=wt_path,
        check=False,
        capture_output=True,
    )


def _clone_build(
    main: Path,
    wt_path: Path,
    build: str,
    symlinked_deps: Sequence[str],
    drop_paths: Collection[str],
) -> bool:
    """APFS COW-clone main/out/<build> into wt_path/out/<build>.

    Returns True on success. Returns False (cleaning up any partial copy) on
    any error or unsupported environment so the caller can fall back to gm.py.
    """
    src_dir = main / "out" / build
    dst_dir = wt_path / "out" / build
    if dst_dir.exists():
        return False

    dst_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        _run(["cp", "-cRPp", str(src_dir), str(dst_dir)])
        # Drop runtime lock/log artifacts inherited from main.
        (dst_dir / ".siso_lock").unlink(missing_ok=True)
        siso_info = dst_dir / "siso.INFO"
        if siso_info.is_symlink():
            siso_info.unlink()

        fs_state = dst_dir / ".siso_fs_state"
        if fs_state.is_file():
            _rewrite_siso_fs_state(
                fs_state,
                old_root=main,
                new_root=wt_path,
                symlinked_deps=symlinked_deps,
                drop_paths=drop_paths,
            )
    except (OSError, subprocess.CalledProcessError, ValueError, _CowUnavailable):
        shutil.rmtree(dst_dir, ignore_errors=True)
        return False

    return True


def _setup_builds(
    wt_path: Path,
    builds: list[str],
    *,
    main: Path | None = None,
    symlinked_deps: Sequence[str] = (),
) -> list[str]:
    """Prepare each build dir.

    On macOS APFS, any warm build directory present in `main/out/` is COW-cloned
    (preserving main's args.gn and rewriting Siso's fs state) so unchanged
    object files share disk blocks and the first build is incremental. All other
    configs fall back to gm.py + symbol_level=2 + remoteexec + gn gen.
    """
    gm = wt_path / "tools" / "dev" / "gm.py"
    results: list[str] = []
    cloned_any = False
    cow_supported: bool | None = None
    drop_paths: set[str] | None = None

    for build in builds:
        label = f"  out/{build}"
        if main is not None:
            src_dir = main / "out" / build
            if _is_warm_build(src_dir):
                if cow_supported is None:
                    cow_supported = _supports_clonefile(src_dir / "args.gn", wt_path)
                if cow_supported:
                    if drop_paths is None:
                        drop_paths = _paths_differing_from_main(main, wt_path)
                    if _clone_build(main, wt_path, build, symlinked_deps, drop_paths):
                        cloned_any = True
                        results.append(f"{label}: ok (cloned from main)")
                        continue

        if not gm.exists():
            results.append(f"{label}: skipped (gm.py not found)")
            continue

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

    if cloned_any and main is not None:
        _sync_mtimes_from_main(main, wt_path, drop_paths or set())

    if not cloned_any and not gm.exists():
        return ["(gm.py not found, skipping build setup)"]
    return results


def _force_cleanup(main: Path, wt_path: Path, branch: str) -> None:
    """Tear down any leftover state for a worktree so a re-create starts clean.

    Handles each leftover independently and best-effort, so it is robust to the
    partial states that make plain `create` brittle: a registered worktree, a
    stray directory git no longer tracks, a stale registration whose dir is
    gone, and a dangling same-named branch. Safe to call when nothing is left.
    """
    registered = any(
        Path(wt.path).resolve() == wt_path.resolve() for wt in list_worktrees(main)
    )
    if registered:
        _remove_external_symlinks(wt_path)
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(wt_path)],
            check=False,
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
        ["git", "worktree", "prune"],
        check=False,
        cwd=main,
        capture_output=True,
        text=True,
    )
    # Delete a dangling same-named branch, unless protected or in use elsewhere.
    if branch not in ("main", "master") and _branch_exists(main, branch):
        in_use = any(
            wt.branch == branch and Path(wt.path).resolve() != wt_path.resolve()
            for wt in list_worktrees(main)
        )
        if not in_use:
            subprocess.run(
                ["git", "branch", "-D", branch],
                check=False,
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
    clone_builds: bool = True,
) -> Created:
    """Create a worktree as a sibling of the main checkout, symlink gclient deps.

    upstream: base branch/ref for the new branch (default "main").
    force: tear down any leftover worktree dir and dangling same-named branch
    first, so re-creating is idempotent (a fresh attempt). Without it, an
    existing path raises.
    clone_builds: on macOS APFS, COW-clone any warm build directories present
    in main/out/ instead of generating them fresh (default True).
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
    linked = _create_symlinks(main, wt_path, gclient_root)
    _skip_v8_worktree_deps(wt_path)

    # Set up default build directories.
    build_results = _setup_builds(
        wt_path,
        _DEFAULT_BUILDS,
        main=main if clone_builds else None,
        symlinked_deps=linked,
    )

    return Created(path=wt_path, builds=build_results)


def _skip_v8_worktree_deps(wt_path: Path) -> None:
    """Turn off V8's own worktree dependency setup for this worktree.

    gm.py runs tools/dev/setup_worktree_build.py on every invocation. When the
    worktree's DEPS differs from the main checkout's, that script runs a
    separate gclient sync into <main>/worktrees/.deps_cache (several GB per
    distinct DEPS, at least three entries kept) and repoints the worktree's
    links into it. Deps here come from _create_symlinks, so the script is
    disabled before _setup_builds first runs gm.py.

    Worktree scope, because a linked worktree shares the common .git/config: a
    plain write would disable it for the main checkout and every other worktree.
    """
    git = ["git", "-C", str(wt_path), "config"]
    _run([*git, "extensions.worktreeConfig", "true"])
    _run([*git, "--worktree", "v8.skip-worktree-deps", "true"])


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


def refresh(repo: Path, name: str) -> Refreshed:
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
    return Refreshed(path=wt_path, linked=linked)


def _remove_external_symlinks(wt_path: Path) -> None:
    """Remove all symlinks under wt_path that point outside of it."""
    resolved_root = str(wt_path.resolve())
    # find -type l is fast: it uses d_type from readdir to skip non-symlinks
    # without stat'ing millions of build artifacts in out/.
    result = subprocess.run(
        ["find", str(wt_path), "-type", "l"],
        check=False,
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
) -> Removed:
    """Remove a worktree: clean up symlinks then git worktree remove.

    If remove_branch is true, also delete the underlying git branch.
    Skipped for detached HEAD, `main`/`master`, or branches checked out
    in another worktree.

    The result describes the branch outcome.
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
        if Path(wt.path).resolve() == wt_path.resolve():
            b = wt.branch
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

    if not remove_branch:
        return Removed(branch=branch, branch_removed=False, note=None)
    if branch is None:
        return Removed(branch, False, "no branch to delete (detached HEAD)")
    if branch in ("main", "master"):
        return Removed(branch, False, f"refusing to delete protected branch {branch!r}")
    for wt in worktrees:
        if Path(wt.path).resolve() == wt_path.resolve():
            continue
        if wt.branch == branch:
            return Removed(
                branch, False, f"branch {branch!r} also checked out in {wt.path}"
            )

    _run(["git", "branch", "-D", branch], cwd=main)
    return Removed(branch=branch, branch_removed=True, note=None)


def list_worktrees(repo: Path) -> list[WorktreeInfo]:
    """Every worktree of `repo`'s repository, the main checkout first."""
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
    return [WorktreeInfo(**wt) for wt in worktrees]


def _branch_exists(repo: Path, branch: str) -> bool:
    r = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/heads/{branch}"],
        check=False,
        cwd=repo,
        capture_output=True,
        text=True,
    )
    return r.returncode == 0
