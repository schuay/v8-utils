"""v8-utils notification daemon.

Polls watched Pinpoint jobs; logs all activity to a log file; sends a
Google Chat notification when a job reaches a terminal state.

Notification methods (in preference order):
  1. Chat app (chat_app_space + chat_service_account_key in config)
  2. Incoming webhook (chat_webhook in config)

New jobs are submitted via a Unix domain socket. The daemon is started
automatically by `pp watch`; it can also be run directly.

State files (under platformdirs.user_data_dir("v8-utils")):
  daemon.pid
  daemon.sock
  daemon.log
  daemon.watched
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path

import httpx
from platformdirs import user_data_dir

from . import config
from . import pinpoint

_STATE_DIR = Path(user_data_dir("v8-utils"))
SOCK_PATH = _STATE_DIR / "daemon.sock"
PID_PATH = _STATE_DIR / "daemon.pid"
LOG_PATH = _STATE_DIR / "daemon.log"
WATCHED_PATH = _STATE_DIR / "daemon.watched"

_TERMINAL_STATES = {"Completed", "Failed", "Cancelled"}
_STARTUP_MTIME = os.path.getmtime(__file__)

log = logging.getLogger("v8-utils")


def _setup_logging() -> None:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(LOG_PATH)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        )
    )
    log.addHandler(handler)
    log.setLevel(logging.WARNING)


# ── Watched-jobs persistence ──────────────────────────────────────────────────


def _save_watched(watched: dict[str, str], lock: threading.Lock) -> None:
    with lock:
        ids = list(watched)
    WATCHED_PATH.write_text(json.dumps(ids))


def _load_watched() -> dict[str, str]:
    try:
        ids = json.loads(WATCHED_PATH.read_text())
        WATCHED_PATH.unlink(missing_ok=True)
        return {jid: jid for jid in ids}
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return {}


# ── Notifications ─────────────────────────────────────────────────────────────


def _format_job_details_for_chat(job: dict) -> str:
    """Format key job fields as a compact Chat-friendly block."""
    args = job.get("arguments", {})
    patch = args.get("experiment_patch")
    if patch:
        subject = pinpoint.fetch_gerrit_subject(patch)
        patch_str = f'{patch}  "{subject}"' if subject else patch
    else:
        patch_str = None
    fields = [
        ("config", job.get("configuration")),
        ("benchmark", args.get("benchmark")),
        ("story", args.get("story")),
        ("patch", patch_str),
        ("base-flags", args.get("base_extra_args")),
        ("exp-flags", args.get("experiment_extra_args")),
    ]
    return "\n".join(f"  {k}: {v}" for k, v in fields if v)


def _format_results_for_chat(rows: list[dict]) -> str:
    """Format results as a Chat-friendly text block with emoji color."""
    sig = [r for r in rows if r.get("significant")]
    if not sig:
        return "\n_No statistically significant changes._"

    def _pct(r: dict) -> float:
        bm = r.get("base_mean") or 0.0
        return ((r.get("exp_mean") or 0.0) - bm) / bm * 100 if bm else 0.0

    sig.sort(key=_pct, reverse=True)

    lines = ["", "*Results (significant):*"]
    for r in sig:
        unit = r.get("unit", "")
        base_mean = r.get("base_mean") or 0.0
        exp_mean = r.get("exp_mean") or 0.0
        if base_mean:
            pct = (exp_mean - base_mean) / base_mean * 100
            pct_str = f"{pct:+.1f}%"
            bigger_is_better = "_biggerIsBetter" in unit
            good = pct > 0 if bigger_is_better else pct < 0
            emoji = "🟢" if good else "🔴"
        else:
            pct_str = "?"
            emoji = "📊"
        lines.append(f"  {emoji} {pct_str} {r['name']}")
    return "\n".join(lines)


def _message_text(job: dict, results: list[dict] | None = None) -> str:
    status = job.get("status", "Unknown")
    name = job.get("name", job.get("job_id", "unknown"))
    job_id = job.get("job_id", "")
    url = f"{pinpoint._PINPOINT_BASE}/job/{job_id}"
    icon = {"Completed": "✅", "Failed": "❌", "Cancelled": "⏹️"}.get(status, "🔔")
    details = _format_job_details_for_chat(job)
    exception = job.get("exception")
    show_cmd = f"`pp show-results {job_id}`"
    text = f"{icon} *{status}*: {name}\n{url}\n{show_cmd}"
    if details:
        text += f"\n{details}"
    if exception:
        text += f"\n\n*Error:* {exception}"
    if results is not None:
        text += _format_results_for_chat(results)
    return text


def _notify_webhook(webhook: str, job: dict, results: list[dict] | None = None) -> None:
    try:
        httpx.post(webhook, json={"text": _message_text(job, results)}, timeout=10)
        log.info("webhook sent for %s", job.get("job_id"))
    except Exception as e:
        log.error("webhook error for %s: %s", job.get("job_id"), e)


def _notify_chat_app(
    space: str, service_account_email: str, job: dict, results: list[dict] | None = None
) -> None:
    from . import chat

    chat.notify(space, service_account_email, _message_text(job, results))
    log.info("Chat app notification sent for %s", job.get("job_id"))


def _notify(cfg: config.Config, job: dict, results: list[dict] | None = None) -> None:
    """Send a notification via Chat app (preferred) or webhook (fallback)."""
    if cfg.chat_app_space and cfg.chat_service_account_email:
        try:
            _notify_chat_app(
                cfg.chat_app_space, cfg.chat_service_account_email, job, results
            )
            return
        except Exception as e:
            log.error("Chat app notification failed: %s", e)
    if cfg.chat_webhook:
        _notify_webhook(cfg.chat_webhook, job, results)


# ── Results poller ────────────────────────────────────────────────────────────

_RESULTS_TIMEOUT = 30 * 60  # seconds to wait for results page after job completes


def _fetch_results_when_ready(job_id: str, poll_interval: int) -> list[dict] | None:
    """Poll until the results page is ready, then return pivot_results.

    Returns None if the results page never appears within _RESULTS_TIMEOUT.
    """
    deadline = time.time() + _RESULTS_TIMEOUT
    while time.time() < deadline:
        time.sleep(poll_interval)
        try:
            return pinpoint.pivot_results(job_id)
        except Exception as e:
            log.info("results not ready for %s: %s", job_id, e)
    log.warning("timed out waiting for results page for %s", job_id)
    return None


def _notify_with_results(cfg: config.Config, job: dict) -> None:
    """Wait for the results page, then send a notification. Runs in its own thread."""
    job_id = job.get("job_id", "")
    try:
        log.info("waiting for results page for %s", job_id)
        results = _fetch_results_when_ready(job_id, cfg.poll_interval)
        _notify(cfg, job, results)
    except Exception:
        log.error("error notifying for %s", job_id, exc_info=True)


# ── Poll loop ─────────────────────────────────────────────────────────────────


def _poll_loop(watched: dict[str, str], lock: threading.Lock) -> None:
    """Periodically poll all watched jobs and notify on terminal status."""
    while True:
        try:
            _poll_loop_inner(watched, lock)
        except Exception:
            log.error("poll loop crashed, restarting in 60s", exc_info=True)
            time.sleep(60)


def _poll_loop_inner(watched: dict[str, str], lock: threading.Lock) -> None:
    cfg = config.load()
    while True:
        time.sleep(cfg.poll_interval)
        try:
            mtime_changed = os.path.getmtime(__file__) != _STARTUP_MTIME
        except OSError:
            mtime_changed = True  # file gone → upgrade happened
        if mtime_changed:
            log.warning("daemon code changed on disk — restarting")
            _restart(watched, lock)
        with lock:
            job_ids = list(watched)
        if not job_ids:
            continue
        log.debug("polling %d job(s): %s", len(job_ids), ", ".join(job_ids))
        for job_id in job_ids:
            try:
                job = pinpoint.fetch_job(job_id)
            except Exception as e:
                log.error("error fetching %s: %s", job_id, e)
                continue
            status = job.get("status", "Unknown")
            log.info("%s  status=%s", job_id, status)
            if status in _TERMINAL_STATES:
                log.info("%s  %s: %s", job_id, status, job.get("name", ""))
                with lock:
                    watched.pop(job_id, None)
                _save_watched(watched, lock)
                if cfg.chat_app_space or cfg.chat_webhook:
                    if status == "Completed":
                        threading.Thread(
                            target=_notify_with_results,
                            args=(cfg, job),
                            daemon=True,
                        ).start()
                    else:
                        _notify(cfg, job, None)


# ── Socket listener ───────────────────────────────────────────────────────────


def _socket_loop(
    watched: dict[str, str], lock: threading.Lock, ready_fd: int | None = None
) -> None:
    """Accept job IDs on the Unix socket and add them to the watch set."""
    SOCK_PATH.unlink(missing_ok=True)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as srv:
        srv.bind(str(SOCK_PATH))
        srv.listen()
        if ready_fd is not None:
            os.write(ready_fd, b"\x00")
            os.close(ready_fd)
        while True:
            try:
                conn, _ = srv.accept()
                with conn:
                    data = conn.recv(256).decode().strip()
                    if not data:
                        continue
                    job_id = pinpoint.job_id_from_url(data)
                    with lock:
                        if job_id not in watched:
                            watched[job_id] = job_id
                            log.info("watching %s", job_id)
                    _save_watched(watched, lock)
            except Exception:
                log.error("socket loop error", exc_info=True)


# ── Daemon entry point ────────────────────────────────────────────────────────


def _restart(watched: dict[str, str], lock: threading.Lock) -> None:
    """Save state, then exec a fresh daemon process with the new code.

    Holds the lock through exec so the socket thread cannot add a job
    between the save and the process replacement.
    """
    lock.acquire()
    WATCHED_PATH.write_text(json.dumps(list(watched)))
    SOCK_PATH.unlink(missing_ok=True)
    PID_PATH.unlink(missing_ok=True)
    os.execv(sys.executable, [sys.executable, "-m", "v8_utils.daemon"])


def _cleanup() -> None:
    SOCK_PATH.unlink(missing_ok=True)
    PID_PATH.unlink(missing_ok=True)


def run(ready_fd: int | None = None) -> None:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(str(os.getpid()))
    _setup_logging()

    watched: dict[str, str] = _load_watched()
    lock = threading.Lock()

    def _shutdown(*_: object) -> None:
        _save_watched(watched, lock)
        _cleanup()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    if watched:
        log.info("restored %d watched job(s): %s", len(watched), ", ".join(watched))
    log.info("started (pid %d)", os.getpid())
    threading.Thread(target=_poll_loop, args=(watched, lock), daemon=True).start()
    _socket_loop(watched, lock, ready_fd=ready_fd)  # blocks


# ── Client helpers (used by pp) ───────────────────────────────────────────────


def is_running() -> bool:
    if not PID_PATH.exists():
        return False
    try:
        pid = int(PID_PATH.read_text())
        os.kill(pid, 0)
        # Detect zombies: they pass the kill-0 check but aren't really alive.
        try:
            state = Path(f"/proc/{pid}/status").read_text()
            if "\nState:\tZ" in state:
                _cleanup_stale()
                return False
        except OSError:
            pass  # non-Linux or no procfs — trust the kill check
        return True
    except (ValueError, ProcessLookupError, PermissionError):
        return False


def _cleanup_stale() -> None:
    """Remove stale PID/socket files left by a dead daemon."""
    PID_PATH.unlink(missing_ok=True)
    SOCK_PATH.unlink(missing_ok=True)


def send_job(job_url: str) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(str(SOCK_PATH))
        s.sendall(job_url.encode())


def start_background() -> None:
    """Fork and start the daemon in the background.

    Uses a double-fork so the daemon is reparented to init/PID 1, preventing
    zombies when the caller (e.g. an MCP server) never waits on children.
    """
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    r_fd, w_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        # First child: setsid + fork again, then exit immediately.
        os.close(r_fd)
        os.setsid()
        pid2 = os.fork()
        if pid2 > 0:
            # Intermediate child exits so the grandchild is reparented to init.
            os._exit(0)
        # Grandchild: this is the actual daemon.
        with open("/dev/null") as devnull:
            os.dup2(devnull.fileno(), 0)
        log_fd = os.open(LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        os.dup2(log_fd, 1)
        os.dup2(log_fd, 2)
        os.close(log_fd)
        run(ready_fd=w_fd)
        os._exit(0)
    # Parent: reap the intermediate child, then wait for daemon readiness.
    os.close(w_fd)
    os.waitpid(pid, 0)
    ready = os.read(r_fd, 1)
    os.close(r_fd)
    if not ready:
        raise RuntimeError("Daemon failed to start")


if __name__ == "__main__":
    run()
