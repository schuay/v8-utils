"""Tests for commits.py — CommitStore."""

import pytest

from cpd.commits import CommitStore


@pytest.fixture
def store(tmp_path):
    s = CommitStore(tmp_path / "test.db")
    yield s
    s.close()


def _insert(
    store: CommitStore, engine: str, commit_id: int, hash: str, date: str, title: str
):
    """Helper to insert a commit directly."""
    store.conn.execute(
        "INSERT INTO commits (engine, hash, commit_id, date, timestamp, title)"
        " VALUES (?, ?, ?, ?, 0, ?)",
        (engine, hash, commit_id, date, title),
    )
    store.conn.commit()


class TestGet:
    def test_found(self, store):
        _insert(store, "v8", 100, "abc123", "2026-01-15", "Fix bug")
        info = store.get("v8", 100)
        assert info is not None
        assert info.id == 100
        assert info.hash == "abc123"
        assert info.title == "Fix bug"

    def test_not_found(self, store):
        assert store.get("v8", 999) is None

    def test_wrong_engine(self, store):
        _insert(store, "v8", 100, "abc123", "2026-01-15", "Fix bug")
        assert store.get("jsc", 100) is None


class TestGetRange:
    def test_basic_range(self, store):
        """get_range returns (after_id, up_to_id] — exclusive lower, inclusive upper."""
        for i in range(10, 16):
            _insert(store, "v8", i, f"hash{i}", f"2026-01-{i}", f"Commit {i}")
        result = store.get_range("v8", 11, 14)
        assert len(result) == 3  # 12, 13, 14
        assert result[0].id == 12
        assert result[-1].id == 14
        result = store.get_range("v8", 11, 12)
        assert len(result) == 1
        assert result[0].id == 12

    def test_empty_range(self, store):
        _insert(store, "v8", 10, "h10", "2026-01-10", "C10")
        assert store.get_range("v8", 10, 10) == []
        assert store.get_range("v8", 100, 200) == []


class TestMaxCommitId:
    def test_with_data(self, store):
        _insert(store, "v8", 100, "h1", "2026-01-01", "C1")
        _insert(store, "v8", 200, "h2", "2026-01-02", "C2")
        assert store.max_commit_id("v8") == 200

    def test_empty(self, store):
        assert store.max_commit_id("v8") is None
