"""Unit tests for pinpoint_cache — SQLite caching layer."""

import concurrent.futures

import pytest

from v8_utils import pinpoint_cache


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    """Give each test its own empty database."""
    monkeypatch.setattr(pinpoint_cache, "_DB_PATH", tmp_path / "cache.db")
    monkeypatch.setattr(pinpoint_cache, "_schema_ready", False)
    pinpoint_cache._local.conn = None
    yield
    pinpoint_cache.close_db()


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


class TestCorruptionRecovery:
    """Auto-recovery when the database file is corrupt."""

    def test_recovers_from_corrupt_db(self, tmp_path):
        # Seed a job, then corrupt the file
        pinpoint_cache.put_job(_make_job("j1", "alice@test.com"))
        assert pinpoint_cache.get_job("j1") is not None

        pinpoint_cache.close_db()
        db_path = tmp_path / "cache.db"
        db_path.write_bytes(b"this is not a sqlite database")

        # Should recover: warn, recreate, and return None (cache miss)
        assert pinpoint_cache.get_job("j1") is None

    def test_put_after_corruption(self, tmp_path):
        db_path = tmp_path / "cache.db"
        db_path.write_bytes(b"corrupt")

        pinpoint_cache.put_job(_make_job("j1", "alice@test.com"))
        assert pinpoint_cache.get_job("j1") is not None


class TestConcurrency:
    """Concurrent thread access must not raise."""

    def test_parallel_writes(self):
        def write(i):
            pinpoint_cache.put_job(
                _make_job(
                    f"j{i}", "alice@test.com", created=f"2026-03-{i + 1:02d}T00:00:00"
                )
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(write, range(20)))

        jobs = pinpoint_cache.query_jobs(users=["alice@test.com"])
        assert len(jobs) == 20

    def test_parallel_reads_and_writes(self):
        # Seed some data
        for i in range(10):
            pinpoint_cache.put_job(
                _make_job(
                    f"j{i}", "bob@test.com", created=f"2026-03-{i + 1:02d}T00:00:00"
                )
            )

        errors = []

        def read_write(i):
            try:
                pinpoint_cache.get_job(f"j{i % 10}")
                pinpoint_cache.put_job(
                    _make_job(
                        f"w{i}", "bob@test.com", created=f"2026-04-{i + 1:02d}T00:00:00"
                    )
                )
            except Exception as e:
                errors.append(e)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(read_write, range(20)))

        assert not errors
