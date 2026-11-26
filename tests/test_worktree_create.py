"""Tests for worktree.create, V8 deps setup, and COW build cloning."""

from __future__ import annotations

import builtins
import gzip
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from v8_utils import worktree

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]


def _git(repo: Path, *args: str) -> tuple[int, str]:
    r = subprocess.run(
        ["git", *args], cwd=repo, check=False, capture_output=True, text=True
    )
    return r.returncode, r.stdout.strip()


def _pb_bytes(field_num: int, payload: bytes) -> bytes:
    tag = (field_num << 3) | 2
    return (
        worktree._encode_varint(tag) + worktree._encode_varint(len(payload)) + payload
    )


def _pb_varint(field_num: int, value: int) -> bytes:
    tag = (field_num << 3) | 0
    return worktree._encode_varint(tag) + worktree._encode_varint(value)


def _make_siso_entry(name: str, *, mtime_ns: int = 1_700_000_000_000_000_000) -> bytes:
    """Build a synthetic Siso hashfs Entry protobuf matching Siso's wire layout."""
    file_id = _pb_varint(1, mtime_ns)
    digest = _pb_bytes(1, b"a" * 64) + _pb_varint(2, 42)
    cmd_hash = b"\x01\x02\x03\x04" * 8
    return (
        _pb_bytes(1, file_id)
        + _pb_bytes(2, name.encode("utf-8"))
        + _pb_bytes(3, digest)
        + _pb_bytes(6, cmd_hash)
        + _pb_varint(8, mtime_ns)
        + _pb_varint(11, 1)
    )


def _make_siso_state(entries: list[bytes]) -> bytes:
    """Build a synthetic Siso hashfs State protobuf with extra top-level fields."""
    out = bytearray()
    for entry in entries:
        out.extend(_pb_bytes(1, entry))
    # Field 3 (last_checked) and field 6 (varint), matching real Siso files.
    out.extend(_pb_bytes(3, _pb_bytes(1, b"d8")))
    out.extend(_pb_varint(6, 1))
    return bytes(out)


def _parse_siso_state(raw: bytes) -> tuple[list[tuple[str, bytes]], list[bytes]]:
    """Return ([(entry_name, raw_entry)], [other_top_level_raw_fields])."""
    dec, _ = worktree._decompress_siso_state(raw)
    entries: list[tuple[str, bytes]] = []
    other: list[bytes] = []
    offset = 0
    while offset < len(dec):
        start = offset
        tag, offset = worktree._read_varint(dec, offset)
        fn, wt = tag >> 3, tag & 7
        if fn == 1 and wt == 2:
            ln, offset = worktree._read_varint(dec, offset)
            entry = dec[offset : offset + ln]
            offset += ln
            # Extract field 2 (name)
            name = ""
            eo = 0
            while eo < len(entry):
                etag, eo = worktree._read_varint(entry, eo)
                efn, ewt = etag >> 3, etag & 7
                if efn == 2 and ewt == 2:
                    eln, eo = worktree._read_varint(entry, eo)
                    name = entry[eo : eo + eln].decode("utf-8")
                    eo += eln
                else:
                    eo = worktree._skip_pb_field(entry, eo, ewt)
            entries.append((name, entry))
        else:
            offset = worktree._skip_pb_field(dec, offset, wt)
            other.append(dec[start:offset])
    return entries, other


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
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

    def fake_setup_builds(wt_path, builds, *, main=None, symlinked_deps=()):
        # gm.py would run here; the flag must already be visible to it.
        seen["flag"] = _git(wt_path, "config", "--get", "v8.skip-worktree-deps")
        seen["main"] = main
        seen["symlinked_deps"] = list(symlinked_deps)
        return []

    monkeypatch.setattr(worktree, "_create_symlinks", lambda *a: ["build"])
    monkeypatch.setattr(worktree, "_setup_builds", fake_setup_builds)

    created = worktree.create(checkout, "wt-one")

    assert seen["flag"] == (0, "true")
    assert seen["main"] == checkout
    assert seen["symlinked_deps"] == ["build"]
    # Worktree scope only: the main checkout keeps V8's default behaviour.
    assert _git(checkout, "config", "--get", "v8.skip-worktree-deps")[0] == 1
    assert created.path == checkout.parent / "wt-one"


@pytest.mark.parametrize("codec", ["gzip", "zstd"])
def test_rewrite_siso_fs_state_relocates_keeps_symlinks_and_drops_differing(
    tmp_path: Path, codec: str
):
    old_root = (tmp_path / "main").resolve()
    new_root = (tmp_path / "feat").resolve()
    old_root.mkdir()
    new_root.mkdir()

    raw_entries = [
        _make_siso_entry(str(old_root)),
        _make_siso_entry(f"{old_root}/out/arm64.optdebug/obj/foo.o"),
        _make_siso_entry(f"{old_root}/src/clean.cc"),
        _make_siso_entry(f"{old_root}/src/dirty.cc"),
        _make_siso_entry(f"{old_root}/build"),
        _make_siso_entry(f"{old_root}/build/config.gni"),
        _make_siso_entry(f"{old_root}/build_overrides/build.gni"),
        _make_siso_entry(f"{old_root}/third_party/abseil-cpp/absl/foo.h"),
        _make_siso_entry(f"{old_root}/third_party/utf8-decoder/utf8.h"),
        _make_siso_entry("/Applications/Xcode.app/SDK/stdio.h"),
    ]
    state_bytes = _make_siso_state(raw_entries)
    compressed = worktree._compress_siso_state(state_bytes, codec)
    fs_state = new_root / ".siso_fs_state"
    fs_state.write_bytes(compressed)

    worktree._rewrite_siso_fs_state(
        fs_state,
        old_root=old_root,
        new_root=new_root,
        symlinked_deps=["build", "third_party/abseil-cpp"],
        drop_paths={"src/dirty.cc"},
    )

    out_raw = fs_state.read_bytes()
    expected_magic = worktree._GZIP_MAGIC if codec == "gzip" else worktree._ZSTD_MAGIC
    assert out_raw.startswith(expected_magic)

    parsed_entries, other = _parse_siso_state(out_raw)
    names = [name for name, _ in parsed_entries]
    assert names == [
        str(new_root),
        f"{new_root}/out/arm64.optdebug/obj/foo.o",
        f"{new_root}/src/clean.cc",
        # src/dirty.cc dropped
        f"{old_root}/build",
        f"{old_root}/build/config.gni",
        # build_overrides must NOT match the "build" symlink prefix
        f"{new_root}/build_overrides/build.gni",
        f"{old_root}/third_party/abseil-cpp/absl/foo.h",
        f"{new_root}/third_party/utf8-decoder/utf8.h",
        "/Applications/Xcode.app/SDK/stdio.h",
    ]
    # Non-entry top-level fields are preserved intact.
    assert other == [_pb_bytes(3, _pb_bytes(1, b"d8")), _pb_varint(6, 1)]


def test_zstd_cli_fallback_when_python_zstd_unavailable(monkeypatch):
    if not shutil.which("zstd"):
        pytest.skip("zstd CLI not installed")

    real_import = builtins.__import__

    def block_zstd(name, *args, **kwargs):
        if name in ("compression", "compression.zstd", "zstandard"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", block_zstd)

    payload = b"hello siso state" * 100
    compressed = worktree._compress_siso_state(payload, "zstd")
    assert compressed.startswith(worktree._ZSTD_MAGIC)
    decompressed, codec = worktree._decompress_siso_state(compressed)
    assert codec == "zstd"
    assert decompressed == payload


def test_rewrite_siso_fs_state_rejects_unknown_format(tmp_path: Path):
    bad = tmp_path / ".siso_fs_state"
    bad.write_bytes(b"not-a-valid-header")
    with pytest.raises(worktree._CowUnavailable):
        worktree._rewrite_siso_fs_state(
            bad,
            old_root=tmp_path / "a",
            new_root=tmp_path / "b",
            symlinked_deps=(),
            drop_paths=(),
        )


def test_is_warm_build_requires_build_log_and_no_lock(tmp_path: Path):
    build_dir = tmp_path / "out" / "arm64.optdebug"
    (build_dir / "obj").mkdir(parents=True)
    (build_dir / "args.gn").write_text("is_debug = true\n")
    (build_dir / "build.ninja").write_text("# ninja\n")

    # Only gn gen'd (no build log): not warm.
    assert not worktree._is_warm_build(build_dir)

    # With .ninja_log: warm.
    (build_dir / ".ninja_log").write_text("# ninja log v6\n")
    assert worktree._is_warm_build(build_dir)

    # With .siso_fs_state instead: warm.
    (build_dir / ".ninja_log").unlink()
    (build_dir / ".siso_fs_state").write_bytes(gzip.compress(b""))
    assert worktree._is_warm_build(build_dir)

    # While an exclusive Siso lock is held: not warm (skipped cleanly).
    if fcntl is not None:
        lock_path = build_dir / ".siso_lock"
        lock_path.write_bytes(b"00000000")
        fd = os.open(lock_path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert not worktree._is_warm_build(build_dir)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        assert worktree._is_warm_build(build_dir)


def test_create_cow_clones_warm_build_and_syncs_mtimes(checkout: Path, monkeypatch):
    # Populate tracked files in main with known historical mtimes.
    (checkout / ".gitignore").write_text("out/\n")
    (checkout / "src").mkdir()
    clean_file = checkout / "src" / "clean.cc"
    dirty_file = checkout / "src" / "dirty.cc"
    branch_file = checkout / "src" / "branch.cc"
    clean_file.write_text("int clean() { return 1; }\n")
    dirty_file.write_text("int dirty() { return 1; }\n")
    branch_file.write_text("int branch() { return 1; }\n")
    assert _git(checkout, "add", ".")[0] == 0
    assert _git(checkout, "commit", "-q", "-m", "add sources")[0] == 0

    # Create a branch that modifies branch.cc relative to main.
    assert _git(checkout, "checkout", "-q", "-b", "feat")[0] == 0
    branch_file.write_text("int branch() { return 2; }\n")
    assert _git(checkout, "commit", "-q", "-am", "edit branch.cc")[0] == 0
    assert _git(checkout, "checkout", "-q", "main")[0] == 0

    # Make dirty.cc modified in main's working tree, and give all three files
    # an unmistakably old mtime so we can verify selective mtime syncing.
    dirty_file.write_text("int dirty() { return 999; }\n")
    old_ns = 1_700_000_000_123_456_789
    for f in (clean_file, dirty_file, branch_file):
        os.utime(f, ns=(old_ns, old_ns))

    # Build one warm config in main/out/<arch>.optdebug.
    build_name = f"{worktree.BUILD_ARCH}.optdebug"
    main_build = checkout / "out" / build_name
    (main_build / "obj").mkdir(parents=True)
    (main_build / "args.gn").write_text("is_debug = true\nsymbol_level = 2\n")
    (main_build / "build.ninja").write_text("# ninja\n")
    (main_build / "obj" / "foo.o").write_bytes(b"ELF-obj-payload")
    (main_build / ".siso_lock").write_bytes(b"12345678")
    (main_build / "siso.INFO").symlink_to("/tmp/nonexistent-siso-log")

    main_resolved = checkout.resolve()
    state_bytes = _make_siso_state(
        [
            _make_siso_entry(f"{main_resolved}/out/{build_name}/obj/foo.o"),
            _make_siso_entry(f"{main_resolved}/src/clean.cc"),
            _make_siso_entry(f"{main_resolved}/src/dirty.cc"),
            _make_siso_entry(f"{main_resolved}/src/branch.cc"),
            _make_siso_entry(f"{main_resolved}/build/config.gni"),
        ]
    )
    main_fs_state = main_build / ".siso_fs_state"
    main_fs_state.write_bytes(worktree._compress_siso_state(state_bytes, "zstd"))
    main_fs_state_before = main_fs_state.read_bytes()

    if not worktree._supports_cow_clone(main_build / "args.gn", checkout.parent):
        pytest.skip("temp filesystem does not support COW cloning")

    monkeypatch.setattr(worktree, "_create_symlinks", lambda *a: ["build"])

    created = worktree.create(checkout, "wt-cow", branch="feat")
    wt = created.path
    wt_resolved = wt.resolve()

    # The warm build was cloned; other configs had no gm.py in this fixture.
    assert f"  out/{build_name}: ok (cloned from main)" in created.builds
    wt_build = wt / "out" / build_name
    assert (wt_build / "obj" / "foo.o").read_bytes() == b"ELF-obj-payload"
    assert not (wt_build / ".siso_lock").exists()
    assert not (wt_build / "siso.INFO").is_symlink()

    # main's .siso_fs_state must never be mutated.
    assert main_fs_state.read_bytes() == main_fs_state_before

    # wt's .siso_fs_state relocated local paths, preserved symlinked deps, and
    # dropped both dirty.cc (dirty in main) and branch.cc (differs on feat).
    parsed, _ = _parse_siso_state((wt_build / ".siso_fs_state").read_bytes())
    assert [name for name, _ in parsed] == [
        f"{wt_resolved}/out/{build_name}/obj/foo.o",
        f"{wt_resolved}/src/clean.cc",
        f"{main_resolved}/build/config.gni",
    ]

    # clean.cc inherited main's nanosecond mtime; dirty.cc and branch.cc kept
    # their fresh checkout mtimes so Siso/Ninja rebuilds them.
    assert (wt / "src" / "clean.cc").stat().st_mtime_ns == old_ns
    assert (wt / "src" / "dirty.cc").stat().st_mtime_ns > old_ns
    assert (wt / "src" / "branch.cc").stat().st_mtime_ns > old_ns

    # Git index was refreshed cleanly after mtime sync (no stat-dirty entries).
    assert _git(wt, "status", "--porcelain") == (0, "")
    assert _git(wt, "diff-files", "--name-only") == (0, "")


def test_create_no_clone_and_fallback_on_corrupt_state(checkout: Path, monkeypatch):
    build_name = f"{worktree.BUILD_ARCH}.optdebug"
    main_build = checkout / "out" / build_name
    main_build.mkdir(parents=True)
    (main_build / "args.gn").write_text("is_debug = true\n")
    (main_build / "build.ninja").write_text("# ninja\n")
    (main_build / ".siso_fs_state").write_bytes(b"corrupt-siso-state")

    if not worktree._supports_cow_clone(main_build / "args.gn", checkout.parent):
        pytest.skip("temp filesystem does not support COW cloning")

    monkeypatch.setattr(worktree, "_create_symlinks", lambda *a: [])

    # Corrupt .siso_fs_state aborts the clone cleanly, removes the partial dir,
    # and falls back to gm.py (not found in this minimal fixture).
    created = worktree.create(checkout, "wt-corrupt")
    assert created.builds == ["(gm.py not found, skipping build setup)"]
    assert not (created.path / "out" / build_name).exists()

    # Valid warm build with clone_builds=False skips cloning altogether.
    (main_build / ".siso_fs_state").write_bytes(gzip.compress(_make_siso_state([])))
    created2 = worktree.create(checkout, "wt-noclone", clone_builds=False)
    assert created2.builds == ["(gm.py not found, skipping build setup)"]
    assert not (created2.path / "out" / build_name).exists()


def test_create_falls_back_when_cow_unsupported(checkout: Path, monkeypatch):
    build_name = f"{worktree.BUILD_ARCH}.optdebug"
    main_build = checkout / "out" / build_name
    main_build.mkdir(parents=True)
    (main_build / "args.gn").write_text("is_debug = true\n")
    (main_build / "build.ninja").write_text("# ninja\n")
    (main_build / ".ninja_log").write_text("# ninja log v6\n")

    monkeypatch.setattr(worktree, "_create_symlinks", lambda *a: [])
    monkeypatch.setattr(worktree, "_supports_cow_clone", lambda *a: False)

    created = worktree.create(checkout, "wt-nocow")
    assert created.builds == ["(gm.py not found, skipping build setup)"]
    assert not (created.path / "out" / build_name).exists()


def test_linux_reflink_probe_and_copy_command(tmp_path: Path, monkeypatch):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    src_file = src / "args.gn"
    src_file.write_text("is_debug = false\n")

    monkeypatch.setattr(sys, "platform", "linux")
    assert worktree._cow_copy_cmd(src, dst / "build") == [
        "cp",
        "-a",
        "--reflink=always",
        str(src),
        str(dst / "build"),
    ]

    seen_cmds: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        seen_cmds.append(cmd)
        # Simulate GNU cp creating the reflinked probe file.
        Path(cmd[-1]).write_bytes(src_file.read_bytes())
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert worktree._supports_cow_clone(src_file, dst) is True
    assert seen_cmds[0][:3] == ["cp", "--reflink=always", "-P"]
    assert seen_cmds[0][3] == str(src_file)
    # Probe file must be cleaned up afterwards.
    assert list(dst.iterdir()) == []

    # Non-reflink FS (e.g. ext4): cp --reflink=always exits non-zero.
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1),
    )
    assert worktree._supports_cow_clone(src_file, dst) is False

    # Unsupported platform returns False / raises _CowUnavailable.
    monkeypatch.setattr(sys, "platform", "win32")
    assert worktree._supports_cow_clone(src_file, dst) is False
    with pytest.raises(worktree._CowUnavailable):
        worktree._cow_copy_cmd(src, dst / "build")
