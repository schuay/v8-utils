"""Tests for the commit-store staleness signal and pd_detect's lazy auto-sync."""

from __future__ import annotations

from v8_utils.mcp_tools import pd as pd_tools
from v8_utils.pd.commits import CommitStore
from v8_utils.pd.models import ChangePoint


def _store(tmp_path) -> CommitStore:
    return CommitStore(tmp_path / "commits.db")


def _cp(
    commit_id: int, prev_commit_id: int = 0, engine: str | None = "v8"
) -> ChangePoint:
    return ChangePoint(
        benchmark="jetstream3.slipstream",
        metric="Total",
        bot="mac-m3-pro-perf",
        variant="v8_default",
        submetric="",
        commit_id=commit_id,
        prev_commit_id=prev_commit_id,
        direction="regression",
        cohens_d=1.0,
        pct_change=-0.05,
        p_value=0.001,
        confidence="high",
        seg_before_mean=100.0,
        seg_after_mean=95.0,
        engine=engine,
    )


def test_max_commit_id_none_when_empty(tmp_path):
    store = _store(tmp_path)
    assert store.max_commit_id("v8") is None


def test_max_commit_id_reports_highest(tmp_path):
    store = _store(tmp_path)
    for cid in (107000, 107342, 107100):
        store.conn.execute(
            "INSERT INTO commits (engine, hash, commit_id) VALUES (?, ?, ?)",
            ("v8", f"h{cid}", cid),
        )
    store.conn.commit()
    assert store.max_commit_id("v8") == 107342
    # Engine-scoped: a jsc row never leaks into the v8 max.
    assert store.max_commit_id("jsc") is None


def test_autosync_triggers_when_point_outruns_store(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.conn.execute(
        "INSERT INTO commits (engine, hash, commit_id) VALUES ('v8', 'h', 107342)"
    )
    store.conn.commit()

    calls = []
    monkeypatch.setattr(
        pd_tools, "sync_engine", lambda s, engine: calls.append(engine) or 0
    )
    # 108483 is past the stored max 107342 -- the real-world skip case.
    pd_tools._autosync_commits(store, [_cp(108483, 108480)], "v8")
    assert calls == ["v8"]


def test_autosync_skips_when_store_is_current(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.conn.execute(
        "INSERT INTO commits (engine, hash, commit_id) VALUES ('v8', 'h', 108500)"
    )
    store.conn.commit()

    calls = []
    monkeypatch.setattr(
        pd_tools, "sync_engine", lambda s, engine: calls.append(engine) or 0
    )
    # An old point below the synced max must not trigger a fruitless resync.
    pd_tools._autosync_commits(store, [_cp(108483, 108480)], "v8")
    assert calls == []


def test_autosync_triggers_on_empty_store(tmp_path, monkeypatch):
    store = _store(tmp_path)
    calls = []
    monkeypatch.setattr(
        pd_tools, "sync_engine", lambda s, engine: calls.append(engine) or 0
    )
    pd_tools._autosync_commits(store, [_cp(200)], "v8")
    assert calls == ["v8"]


def test_autosync_uses_default_engine_when_point_untagged(tmp_path, monkeypatch):
    store = _store(tmp_path)
    calls = []
    monkeypatch.setattr(
        pd_tools, "sync_engine", lambda s, engine: calls.append(engine) or 0
    )
    pd_tools._autosync_commits(store, [_cp(200, engine=None)], "jsc")
    assert calls == ["jsc"]


def test_autosync_no_engine_no_sync(tmp_path, monkeypatch):
    store = _store(tmp_path)
    calls = []
    monkeypatch.setattr(
        pd_tools, "sync_engine", lambda s, engine: calls.append(engine) or 0
    )
    # Neither the point nor the source names an engine -- nothing to sync against.
    pd_tools._autosync_commits(store, [_cp(200, engine=None)], None)
    assert calls == []


def test_autosync_syncs_each_engine_once(tmp_path, monkeypatch):
    store = _store(tmp_path)
    calls = []
    monkeypatch.setattr(
        pd_tools, "sync_engine", lambda s, engine: calls.append(engine) or 0
    )
    results = [_cp(200, engine="v8"), _cp(300, engine="v8"), _cp(150, engine="jsc")]
    pd_tools._autosync_commits(store, results, None)
    assert sorted(calls) == ["jsc", "v8"]


def test_autosync_swallows_sync_failure(tmp_path, monkeypatch):
    store = _store(tmp_path)

    def boom(s, engine):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(pd_tools, "sync_engine", boom)
    # Best-effort: a sync failure must not propagate into the detect call.
    pd_tools._autosync_commits(store, [_cp(200)], "v8")
