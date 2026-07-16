"""Tests for sync_engine: the shared fetch+populate path with a bounded fetch."""

from __future__ import annotations

import subprocess

from v8_utils.pd import engines


class _FakeStore:
    def __init__(self):
        self.populate_calls = []

    def populate(self, engine, src_dir, id_regex, since=None, path=None):
        self.populate_calls.append((engine, since))
        return 7


def test_sync_engine_unknown_engine_returns_zero(monkeypatch):
    store = _FakeStore()
    assert engines.sync_engine(store, "nonesuch") == 0
    assert store.populate_calls == []


def test_sync_engine_missing_checkout_returns_zero(monkeypatch):
    monkeypatch.setattr(engines, "get_src_dir", lambda engine: None)
    store = _FakeStore()
    assert engines.sync_engine(store, "v8") == 0
    assert store.populate_calls == []


def test_sync_engine_fetch_failure_still_populates(monkeypatch, tmp_path):
    monkeypatch.setattr(engines, "get_src_dir", lambda engine: tmp_path)

    def fake_run(*a, **kw):
        return subprocess.CompletedProcess(a, returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(engines.subprocess, "run", fake_run)
    store = _FakeStore()
    # A failed fetch is logged, not fatal: populate still runs on the checkout.
    assert engines.sync_engine(store, "v8") == 7
    assert store.populate_calls == [("v8", "6 months ago")]


def test_sync_engine_fetch_timeout_still_populates(monkeypatch, tmp_path):
    monkeypatch.setattr(engines, "get_src_dir", lambda engine: tmp_path)

    def hang(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="git fetch", timeout=kw.get("timeout"))

    monkeypatch.setattr(engines.subprocess, "run", hang)
    store = _FakeStore()
    # A hung remote must degrade to "use the checkout as-is", never propagate.
    assert engines.sync_engine(store, "v8") == 7
    assert store.populate_calls == [("v8", "6 months ago")]


def test_sync_engine_passes_fetch_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr(engines, "get_src_dir", lambda engine: tmp_path)
    seen = {}

    def capture(*a, **kw):
        seen["timeout"] = kw.get("timeout")
        return subprocess.CompletedProcess(a, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(engines.subprocess, "run", capture)
    engines.sync_engine(_FakeStore(), "v8")
    assert seen["timeout"] == engines._FETCH_TIMEOUT_S


def test_sync_engine_no_fetch_skips_subprocess(monkeypatch, tmp_path):
    monkeypatch.setattr(engines, "get_src_dir", lambda engine: tmp_path)

    def boom(*a, **kw):
        raise AssertionError("subprocess.run must not be called when fetch=False")

    monkeypatch.setattr(engines.subprocess, "run", boom)
    store = _FakeStore()
    assert engines.sync_engine(store, "v8", fetch=False) == 7
