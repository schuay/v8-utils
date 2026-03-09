"""v8-mcp notification daemon.

Polls watched Pinpoint jobs and sends a Google Chat webhook notification
when each job reaches a terminal state (Completed, Failed, Cancelled).

New jobs are submitted via a Unix domain socket. The daemon is started
automatically by `pp watch`; it can also be run directly.

State files:
  ~/.local/share/v8-mcp/daemon.pid
  ~/.local/share/v8-mcp/daemon.sock
"""

from __future__ import annotations

import json
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

_STATE_DIR = Path("~/.local/share/v8-mcp").expanduser()
SOCK_PATH = _STATE_DIR / "daemon.sock"
PID_PATH  = _STATE_DIR / "daemon.pid"

_TERMINAL_STATES = {"Completed", "Failed", "Cancelled"}


# ── Webhook notification ───────────────────────────────────────────────────────

def _notify(webhook: str, job: dict) -> None:
    status = job.get("status", "Unknown")
    name   = job.get("name", job.get("job_id", "unknown"))
    job_id = job.get("job_id", "")
    url    = f"{pinpoint._PINPOINT_BASE}/job/{job_id}"
    icon   = {"Completed": "✅", "Failed": "❌", "Cancelled": "⏹️"}.get(status, "🔔")
    text   = f"{icon} *{status}*: {name}\n{url}"
    try:
        httpx.post(webhook, json={"text": text}, timeout=10)
    except Exception as e:
        print(f"[daemon] webhook error: {e}", file=sys.stderr)


# ── Poll loop ─────────────────────────────────────────────────────────────────

def _poll_loop(watched: dict[str, str], lock: threading.Lock) -> None:
    """Periodically poll all watched jobs and notify on terminal status."""
    cfg = config.load()
    while True:
        time.sleep(cfg.poll_interval)
        with lock:
            job_ids = list(watched)
        for job_id in job_ids:
            try:
                job = pinpoint.fetch_job(job_id)
            except Exception as e:
                print(f"[daemon] error fetching {job_id}: {e}", file=sys.stderr)
                continue
            status = job.get("status", "")
            if status in _TERMINAL_STATES:
                if cfg.chat_webhook:
                    _notify(cfg.chat_webhook, job)
                else:
                    print(f"[daemon] {status}: {job.get('name')} — no webhook configured",
                          file=sys.stderr)
                with lock:
                    watched.pop(job_id, None)


# ── Socket listener ───────────────────────────────────────────────────────────

def _socket_loop(watched: dict[str, str], lock: threading.Lock) -> None:
    """Accept job IDs on the Unix socket and add them to the watch set."""
    SOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
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
                        print(f"[daemon] watching {job_id}", flush=True)


# ── Daemon entry point ────────────────────────────────────────────────────────

def _write_pid() -> None:
    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(str(os.getpid()))


def _cleanup() -> None:
    SOCK_PATH.unlink(missing_ok=True)
    PID_PATH.unlink(missing_ok=True)


def run() -> None:
    _write_pid()
    signal.signal(signal.SIGTERM, lambda *_: (_cleanup(), sys.exit(0)))
    signal.signal(signal.SIGINT,  lambda *_: (_cleanup(), sys.exit(0)))

    watched: dict[str, str] = {}
    lock = threading.Lock()

    poll_thread = threading.Thread(target=_poll_loop, args=(watched, lock), daemon=True)
    poll_thread.start()

    print(f"[daemon] started (pid {os.getpid()}, socket {SOCK_PATH})", flush=True)
    _socket_loop(watched, lock)  # blocks


# ── Client helpers (used by pp) ───────────────────────────────────────────────

def is_running() -> bool:
    """Return True if a daemon process is alive."""
    if not PID_PATH.exists():
        return False
    try:
        pid = int(PID_PATH.read_text())
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError):
        return False


def send_job(job_url: str) -> None:
    """Send a job URL/ID to the running daemon."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(str(SOCK_PATH))
        s.sendall(job_url.encode())


def start_background() -> None:
    """Fork and start the daemon in the background."""
    pid = os.fork()
    if pid == 0:
        # Child: detach and run
        os.setsid()
        # Redirect stdio so the parent terminal isn't polluted
        for fd, path in [(0, "/dev/null"), (1, "/dev/null"), (2, "/dev/null")]:
            f = open(path, "r" if fd == 0 else "a")
            os.dup2(f.fileno(), fd)
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
