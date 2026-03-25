"""Unit tests for pinpoint_cache — SQLite caching layer."""

import tempfile
from pathlib import Path

import pytest

from v8_utils import pinpoint_cache


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    """Give each test its own empty database."""
    monkeypatch.setattr(pinpoint_cache, "_DB_PATH", tmp_path / "cache.db")
    monkeypatch.setattr(pinpoint_cache, "_db", None)
    yield


def _make_job(job_id, user, status="Completed", created="2026-03-01T00:00:00"):
    return {
        "job_id": job_id,
        "user": user,
        "status": status,
        "created": created,
    }


class TestPrune:
    """prune() must not violate NOT NULL when all jobs for a user are removed."""

    def test_prune_removes_watermark_when_all_jobs_pruned(self):
        old = "2020-01-01T00:00:00"
        pinpoint_cache.put_jobs([_make_job("j1", "alice@test.com", created=old)])
        pinpoint_cache.set_range("alice@test.com", old, old)

        pinpoint_cache.prune()

        assert pinpoint_cache.get_range("alice@test.com") == (None, None)

    def test_prune_updates_floor_for_remaining_jobs(self):
        old = "2020-01-01T00:00:00"
        recent = "2099-01-01T00:00:00"
        pinpoint_cache.put_jobs(
            [
                _make_job("j1", "bob@test.com", created=old),
                _make_job("j2", "bob@test.com", created=recent),
            ]
        )
        pinpoint_cache.set_range("bob@test.com", recent, old)

        pinpoint_cache.prune()

        assert pinpoint_cache.get_range("bob@test.com") == (recent, recent)

    def test_prune_mixed_users(self):
        """One user fully pruned, another partially pruned."""
        old = "2020-01-01T00:00:00"
        recent = "2099-01-01T00:00:00"
        pinpoint_cache.put_jobs(
            [
                _make_job("j1", "alice@test.com", created=old),
                _make_job("j2", "bob@test.com", created=old),
                _make_job("j3", "bob@test.com", created=recent),
            ]
        )
        pinpoint_cache.set_range("alice@test.com", old, old)
        pinpoint_cache.set_range("bob@test.com", recent, old)

        pinpoint_cache.prune()

        assert pinpoint_cache.get_range("alice@test.com") == (None, None)
        assert pinpoint_cache.get_range("bob@test.com") == (recent, recent)
