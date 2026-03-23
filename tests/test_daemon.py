"""Tests for daemon persistence, restart, poll loop, and socket integration."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from v8_utils import config, daemon
from v8_utils.daemon import (
    _load_watched,
    _poll_loop_inner,
    _restart,
    _save_watched,
    _socket_loop,
    send_job,
)


def _cfg(**kw):
    defaults = {"poll_interval": 1, "chat_webhook": "https://hook"}
    defaults.update(kw)
    return config.Config(**defaults)


def _job_response(job_id, status, **kw):
    return {"job_id": job_id, "status": status, "name": "Test Job", **kw}


@pytest.fixture()
def _daemon_paths(tmp_path, monkeypatch):
    """Redirect all daemon state files to tmp_path."""
    monkeypatch.setattr(daemon, "WATCHED_PATH", tmp_path / "daemon.watched")
    monkeypatch.setattr(daemon, "SOCK_PATH", tmp_path / "daemon.sock")
    monkeypatch.setattr(daemon, "PID_PATH", tmp_path / "daemon.pid")


# ── Save / Load persistence ──────────────────────────────────────────────────


class TestSaveLoadWatched:
    def test_round_trip(self, _daemon_paths):
        watched = {"abc": "abc", "def": "def"}
        lock = threading.Lock()
        _save_watched(watched, lock)
        loaded = _load_watched()
        assert loaded == watched
        assert not daemon.WATCHED_PATH.exists()

    def test_load_missing_file(self, _daemon_paths):
        assert _load_watched() == {}

    def test_load_corrupt_json(self, _daemon_paths):
        daemon.WATCHED_PATH.write_text("not valid json{{")
        assert _load_watched() == {}

    def test_save_acquires_lock(self, _daemon_paths):
        lock = threading.Lock()
        lock.acquire()
        done = threading.Event()

        def save():
            _save_watched({"x": "x"}, lock)
            done.set()

        t = threading.Thread(target=save)
        t.start()
        time.sleep(0.05)
        assert not daemon.WATCHED_PATH.exists()
        lock.release()
        done.wait(timeout=2)
        assert json.loads(daemon.WATCHED_PATH.read_text()) == ["x"]
        t.join()


# ── Restart ───────────────────────────────────────────────────────────────────


class TestRestart:
    def test_writes_state_and_execs(self, _daemon_paths):
        daemon.SOCK_PATH.write_text("")
        daemon.PID_PATH.write_text("1234")

        with patch("os.execv", side_effect=SystemExit) as mock_exec:
            with pytest.raises(SystemExit):
                _restart({"abc": "abc"}, threading.Lock())
        assert json.loads(daemon.WATCHED_PATH.read_text()) == ["abc"]
        assert not daemon.SOCK_PATH.exists()
        assert not daemon.PID_PATH.exists()
        mock_exec.assert_called_once_with(
            sys.executable, [sys.executable, "-m", "v8_utils.daemon"]
        )

    def test_holds_lock(self, _daemon_paths):
        lock = threading.Lock()
        lock_was_held = []

        def check_lock(*_args):
            lock_was_held.append(lock.locked())
            raise SystemExit

        with patch("os.execv", side_effect=check_lock):
            with pytest.raises(SystemExit):
                _restart({"abc": "abc"}, lock)
        assert lock_was_held == [True]


# ── Poll loop ─────────────────────────────────────────────────────────────────


class TestPollLoopInner:
    """Tests for _poll_loop_inner.

    Each test patches time.sleep to break the infinite loop after the
    desired number of iterations by raising StopIteration.
    """

    def _run(self, watched, lock, *, iterations=1, sleep_raises=StopIteration):
        """Run _poll_loop_inner for a controlled number of iterations."""
        call_count = 0

        def fake_sleep(_seconds):
            nonlocal call_count
            call_count += 1
            if call_count > iterations:
                raise sleep_raises

        with patch("v8_utils.daemon.time.sleep", side_effect=fake_sleep):
            with pytest.raises(sleep_raises):
                _poll_loop_inner(watched, lock)

    def test_mtime_change_triggers_restart(self, _daemon_paths):
        watched = {"j1": "j1"}
        lock = threading.Lock()
        with (
            patch("v8_utils.daemon.config.load", return_value=_cfg()),
            patch("v8_utils.daemon.os.path.getmtime", return_value=-1),
            patch("v8_utils.daemon.time.sleep", side_effect=lambda _: None),
            patch("os.execv", side_effect=SystemExit),
        ):
            with pytest.raises(SystemExit):
                _poll_loop_inner(watched, lock)
        assert json.loads(daemon.WATCHED_PATH.read_text()) == ["j1"]

    def test_empty_watched_skips_fetch(self, _daemon_paths):
        watched = {}
        lock = threading.Lock()
        with (
            patch("v8_utils.daemon.config.load", return_value=_cfg()),
            patch(
                "v8_utils.daemon.os.path.getmtime",
                return_value=daemon._STARTUP_MTIME,
            ),
            patch("v8_utils.daemon.pinpoint.fetch_job") as mock_fetch,
        ):
            self._run(watched, lock)
        mock_fetch.assert_not_called()

    def test_fetch_error_continues(self, _daemon_paths):
        watched = {"j1": "j1"}
        lock = threading.Lock()
        with (
            patch("v8_utils.daemon.config.load", return_value=_cfg()),
            patch(
                "v8_utils.daemon.os.path.getmtime",
                return_value=daemon._STARTUP_MTIME,
            ),
            patch(
                "v8_utils.daemon.pinpoint.fetch_job",
                side_effect=RuntimeError("network"),
            ),
            patch("v8_utils.daemon._notify") as mock_notify,
        ):
            self._run(watched, lock)
        assert "j1" in watched
        mock_notify.assert_not_called()

    def test_completed_removed_and_notified(self, _daemon_paths):
        watched = {"j1": "j1"}
        lock = threading.Lock()
        threads_started = []

        def fake_thread(**kwargs):
            t = MagicMock()
            threads_started.append(kwargs)
            return t

        with (
            patch("v8_utils.daemon.config.load", return_value=_cfg()),
            patch(
                "v8_utils.daemon.os.path.getmtime",
                return_value=daemon._STARTUP_MTIME,
            ),
            patch(
                "v8_utils.daemon.pinpoint.fetch_job",
                return_value=_job_response("j1", "Completed"),
            ),
            patch("v8_utils.daemon.threading.Thread", side_effect=fake_thread),
        ):
            self._run(watched, lock)
        assert "j1" not in watched
        assert daemon.WATCHED_PATH.exists()
        assert len(threads_started) == 1
        assert threads_started[0]["target"].__name__ == "_notify_with_results"

    def test_failed_removed_and_notified(self, _daemon_paths):
        watched = {"j1": "j1"}
        lock = threading.Lock()
        with (
            patch("v8_utils.daemon.config.load", return_value=_cfg()),
            patch(
                "v8_utils.daemon.os.path.getmtime",
                return_value=daemon._STARTUP_MTIME,
            ),
            patch(
                "v8_utils.daemon.pinpoint.fetch_job",
                return_value=_job_response("j1", "Failed"),
            ),
            patch("v8_utils.daemon._notify") as mock_notify,
        ):
            self._run(watched, lock)
        assert "j1" not in watched
        mock_notify.assert_called_once()

    def test_cancelled_removed(self, _daemon_paths):
        watched = {"j1": "j1"}
        lock = threading.Lock()
        with (
            patch("v8_utils.daemon.config.load", return_value=_cfg()),
            patch(
                "v8_utils.daemon.os.path.getmtime",
                return_value=daemon._STARTUP_MTIME,
            ),
            patch(
                "v8_utils.daemon.pinpoint.fetch_job",
                return_value=_job_response("j1", "Cancelled"),
            ),
            patch("v8_utils.daemon._notify") as mock_notify,
        ):
            self._run(watched, lock)
        assert "j1" not in watched
        mock_notify.assert_called_once()

    def test_running_not_removed(self, _daemon_paths):
        watched = {"j1": "j1"}
        lock = threading.Lock()
        with (
            patch("v8_utils.daemon.config.load", return_value=_cfg()),
            patch(
                "v8_utils.daemon.os.path.getmtime",
                return_value=daemon._STARTUP_MTIME,
            ),
            patch(
                "v8_utils.daemon.pinpoint.fetch_job",
                return_value=_job_response("j1", "Running"),
            ),
            patch("v8_utils.daemon._notify") as mock_notify,
        ):
            self._run(watched, lock)
        assert "j1" in watched
        mock_notify.assert_not_called()

    def test_multiple_jobs_one_terminal(self, _daemon_paths):
        watched = {"j1": "j1", "j2": "j2"}
        lock = threading.Lock()

        def fetch(jid):
            if jid == "j1":
                return _job_response("j1", "Running")
            return _job_response("j2", "Completed")

        with (
            patch("v8_utils.daemon.config.load", return_value=_cfg()),
            patch(
                "v8_utils.daemon.os.path.getmtime",
                return_value=daemon._STARTUP_MTIME,
            ),
            patch("v8_utils.daemon.pinpoint.fetch_job", side_effect=fetch),
            patch("v8_utils.daemon.threading.Thread", return_value=MagicMock()),
        ):
            self._run(watched, lock)
        assert "j1" in watched
        assert "j2" not in watched

    def test_no_notify_without_chat_config(self, _daemon_paths):
        watched = {"j1": "j1"}
        lock = threading.Lock()
        with (
            patch(
                "v8_utils.daemon.config.load",
                return_value=_cfg(chat_webhook=None),
            ),
            patch(
                "v8_utils.daemon.os.path.getmtime",
                return_value=daemon._STARTUP_MTIME,
            ),
            patch(
                "v8_utils.daemon.pinpoint.fetch_job",
                return_value=_job_response("j1", "Completed"),
            ),
            patch("v8_utils.daemon._notify") as mock_notify,
            patch("v8_utils.daemon.threading.Thread") as mock_thread,
        ):
            self._run(watched, lock)
        assert "j1" not in watched
        mock_notify.assert_not_called()
        mock_thread.assert_not_called()


# ── Socket integration ────────────────────────────────────────────────────────


class TestSocketIntegration:
    def _start_socket(self, watched, lock):
        r_fd, w_fd = os.pipe()
        t = threading.Thread(
            target=_socket_loop, args=(watched, lock, w_fd), daemon=True
        )
        t.start()
        os.read(r_fd, 1)  # wait for ready
        os.close(r_fd)
        return t

    def test_send_and_receive(self, _daemon_paths):
        watched = {}
        lock = threading.Lock()
        self._start_socket(watched, lock)
        send_job("https://pinpoint-dot-chromeperf.appspot.com/job/abc123")
        time.sleep(0.1)
        assert "abc123" in watched

    def test_multiple_jobs(self, _daemon_paths):
        watched = {}
        lock = threading.Lock()
        self._start_socket(watched, lock)
        for jid in ("aaa", "bbb", "ccc"):
            send_job(f"https://pinpoint-dot-chromeperf.appspot.com/job/{jid}")
        time.sleep(0.2)
        assert watched == {"aaa": "aaa", "bbb": "bbb", "ccc": "ccc"}

    def test_duplicate_ignored(self, _daemon_paths):
        watched = {}
        lock = threading.Lock()
        self._start_socket(watched, lock)
        send_job("https://pinpoint-dot-chromeperf.appspot.com/job/dup")
        send_job("https://pinpoint-dot-chromeperf.appspot.com/job/dup")
        time.sleep(0.1)
        assert len(watched) == 1
