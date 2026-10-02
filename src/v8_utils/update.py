"""Startup update prompt shared by interactive CLIs."""

from __future__ import annotations

import fcntl
import json
import os
from importlib import metadata
from pathlib import Path
import subprocess
import sys
import time

import httpx
from platformdirs import user_cache_dir

REPOSITORY = "https://github.com/schuay/v8-utils.git"
VERSION_URL = "https://raw.githubusercontent.com/schuay/v8-utils/main/VERSION"
CHECK_INTERVAL = 24 * 60 * 60


def install_command() -> list[str]:
    return [
        "uv",
        "tool",
        "install",
        f"v8-utils @ git+{REPOSITORY}",
        "--reinstall",
        "--index-url",
        "https://pypi.org/simple/",
    ]


def _stamp(text: str) -> int:
    text = text.strip()
    if not text or len(text) > 20 or not text.isascii() or not text.isdecimal():
        raise ValueError("VERSION must contain a nonnegative integer")
    return int(text)


def current_version() -> int:
    path = Path(__file__).with_name("VERSION")
    if not path.exists():
        path = Path(__file__).resolve().parents[2] / "VERSION"
    return _stamp(path.read_text())


def latest_version() -> int:
    """Fetch the upstream stamp; callers decide how to handle failures."""
    with httpx.stream("GET", VERSION_URL, timeout=2.0) as response:
        response.raise_for_status()
        body = b""
        for chunk in response.iter_bytes():
            body += chunk
            if len(body) > 64:
                raise ValueError("VERSION is too large")
    return _stamp(body.decode("ascii"))


def _eligible_install() -> bool:
    # Only replace the full distribution in a uv tool environment. Core installs
    # and local, editable, fork, branch, or pinned Git installs retain their source.
    if not (Path(sys.prefix) / "uv-receipt.toml").is_file():
        return False
    try:
        dist = metadata.distribution("v8-utils")
        if (
            Path(dist.locate_file("v8_utils/update.py")).resolve()
            != Path(__file__).resolve()
        ):
            return False
        direct_url = dist.read_text("direct_url.json")
        if direct_url is None:
            return True
        source = json.loads(direct_url)
        vcs = source.get("vcs_info", {})
        return (
            source.get("url") == REPOSITORY
            and vcs.get("vcs") == "git"
            and vcs.get("requested_revision") in (None, "main")
            and not source.get("dir_info")
            and not source.get("subdirectory")
        )
    except (metadata.PackageNotFoundError, ValueError, AttributeError, TypeError):
        return False


def _read_state(path: Path) -> dict:
    try:
        state = json.loads(path.read_text())
        if not isinstance(state, dict):
            return {}
        return {
            key: value
            for key, value in state.items()
            if key in ("checked_at", "attempted") and type(value) in (int, float)
        }
    except (OSError, ValueError):
        return {}


def _write_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(path)


def check_for_update() -> None:
    """Offer an update before command dispatch; exit if installation is attempted."""
    if os.environ.get("V8_UTILS_NO_AUTO_UPDATE") == "1":
        return
    if not all(stream.isatty() for stream in (sys.stdin, sys.stdout, sys.stderr)):
        return

    from . import config

    try:
        if not config.load().auto_update or not _eligible_install():
            return
        current = current_version()
        cache = Path(user_cache_dir("v8-utils"))
        cache.mkdir(parents=True, exist_ok=True)
        with (cache / "update.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            state_path = cache / "update.json"
            state = _read_state(state_path)
            now = time.time()
            if 0 <= now - state.get("checked_at", 0) < CHECK_INTERVAL:
                return
            latest = latest_version()
            state["checked_at"] = now
            _write_state(state_path, state)
            if latest <= current or latest <= state.get("attempted", -1):
                return
            print(
                f"v8-utils update available ({current} -> {latest}). Update from GitHub? [y/N] ",
                end="",
                file=sys.stderr,
                flush=True,
            )
            if sys.stdin.readline().strip().lower() not in ("y", "yes"):
                return
            state["attempted"] = latest
            _write_state(state_path, state)
            print("Updating v8-utils...", file=sys.stderr, flush=True)
            try:
                result = subprocess.run(
                    install_command(), stdout=sys.stderr, stderr=sys.stderr
                )
            except OSError as exc:
                print(
                    f"Update failed: {exc}. Retry with `pp upgrade`.", file=sys.stderr
                )
                raise SystemExit(1) from exc
            if result.returncode:
                print("Update failed. Retry with `pp upgrade`.", file=sys.stderr)
                raise SystemExit(result.returncode)
            print("Updated v8-utils. Rerun your command.", file=sys.stderr)
            raise SystemExit(0)
    except (OSError, ValueError, httpx.HTTPError):
        # An unavailable update service or unwritable cache must not block work.
        return
