"""v8-utils notification daemon.

Polls watched Pinpoint jobs; logs all activity to a log file; sends a
Google Chat notification when a job reaches a terminal state.

Notification methods (in preference order):
  1. Chat app (chat_app_space + chat_service_account_key in config)
  2. Incoming webhook (chat_webhook in config)

New jobs are submitted via a Unix domain socket. The daemon is started
automatically by `pp watch`; it can also be run directly.

State files:
  ~/.local/share/v8-utils/daemon.pid
  ~/.local/share/v8-utils/daemon.sock
  ~/.local/share/v8-utils/daemon.log
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path

import httpx

import config
import pinpoint

_STATE_DIR = Path("~/.local/share/v8-utils").expanduser()
SOCK_PATH = _STATE_DIR / "daemon.sock"
PID_PATH  = _STATE_DIR / "daemon.pid"
LOG_PATH  = _STATE_DIR / "daemon.log"

_TERMINAL_STATES = {"Completed", "Failed", "Cancelled"}

log = logging.getLogger("v8-utils")


def _setup_logging() -> None:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(LOG_PATH)
    handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s",
                                           datefmt="%Y-%m-%d %H:%M:%S"))
    log.addHandler(handler)
    log.setLevel(logging.WARNING)


# ── Notifications ─────────────────────────────────────────────────────────────

def _message_text(job: dict) -> str:
    status = job.get("status", "Unknown")
    name   = job.get("name", job.get("job_id", "unknown"))
    job_id = job.get("job_id", "")
    url    = f"{pinpoint._PINPOINT_BASE}/job/{job_id}"
    icon   = {"Completed": "✅", "Failed": "❌", "Cancelled": "⏹️"}.get(status, "🔔")
    return f"{icon} *{status}*: {name}\n{url}"


def _notify_webhook(webhook: str, job: dict) -> None:
    try:
        httpx.post(webhook, json={"text": _message_text(job)}, timeout=10)
        log.info("webhook sent for %s", job.get("job_id"))
    except Exception as e:
        log.error("webhook error for %s: %s", job.get("job_id"), e)


def _notify_chat_app(space: str, key_path: str, job: dict) -> None:
    # TODO: implement service account auth + Chat REST API call
    # 1. Load service account JSON from key_path
    # 2. Mint a short-lived access token (google-auth)
    # 3. POST to https://chat.googleapis.com/v1/{space}/messages
    raise NotImplementedError("Chat app notifications not yet implemented")


def _notify(cfg: config.Config, job: dict) -> None:
    """Send a notification via Chat app (preferred) or webhook (fallback)."""
    if cfg.chat_app_space and cfg.chat_service_account_key:
        try:
            _notify_chat_app(cfg.chat_app_space, cfg.chat_service_account_key, job)
            return
        except NotImplementedError:
            raise
        except Exception as e:
            log.error("Chat app notification failed: %s", e)
    if cfg.chat_webhook:
        _notify_webhook(cfg.chat_webhook, job)


# ── Poll loop ─────────────────────────────────────────────────────────────────

def _poll_loop(watched: dict[str, str], lock: threading.Lock) -> None:
    """Periodically poll all watched jobs and notify on terminal status."""
    cfg = config.load()
    while True:
        time.sleep(cfg.poll_interval)
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
                if cfg.chat_app_space or cfg.chat_webhook:
                    _notify(cfg, job)
                with lock:
                    watched.pop(job_id, None)


# ── Socket listener ───────────────────────────────────────────────────────────

def _socket_loop(watched: dict[str, str], lock: threading.Lock) -> None:
    """Accept job IDs on the Unix socket and add them to the watch set."""
    SOCK_PATH.unlink(missing_ok=True)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as srv:
        srv.bind(str(SOCK_PATH))
        srv.listen()
        while True:
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


# ── Daemon entry point ────────────────────────────────────────────────────────

def _cleanup() -> None:
    SOCK_PATH.unlink(missing_ok=True)
    PID_PATH.unlink(missing_ok=True)


def run() -> None:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(str(os.getpid()))
    _setup_logging()

    signal.signal(signal.SIGTERM, lambda *_: (_cleanup(), sys.exit(0)))
    signal.signal(signal.SIGINT,  lambda *_: (_cleanup(), sys.exit(0)))

    watched: dict[str, str] = {}
    lock = threading.Lock()

    log.info("started (pid %d)", os.getpid())
    threading.Thread(target=_poll_loop, args=(watched, lock), daemon=True).start()
    _socket_loop(watched, lock)  # blocks


# ── Client helpers (used by pp) ───────────────────────────────────────────────

def is_running() -> bool:
    if not PID_PATH.exists():
        return False
    try:
        pid = int(PID_PATH.read_text())
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError):
        return False


def send_job(job_url: str) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(str(SOCK_PATH))
        s.sendall(job_url.encode())


def start_background() -> None:
    """Fork and start the daemon in the background."""
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    pid = os.fork()
    if pid == 0:
        os.setsid()
        with open("/dev/null") as devnull:
            os.dup2(devnull.fileno(), 0)
        log_fd = os.open(LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        os.dup2(log_fd, 1)
        os.dup2(log_fd, 2)
        os.close(log_fd)
        run()
        sys.exit(0)
    # Parent: wait briefly for socket to appear
    for _ in range(20):
        if SOCK_PATH.exists():
            return
        time.sleep(0.1)
    raise RuntimeError("Daemon did not start in time")


if __name__ == "__main__":
    run()
