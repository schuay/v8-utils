"""SQLite cache for Pinpoint job listings and results.

Stores raw API job dicts and computed pivot_results rows so that
repeated queries avoid re-fetching terminal (Completed/Failed/Cancelled)
jobs from the Pinpoint API.

Database location: ~/.local/share/v8-utils/cache.db

Each thread gets its own SQLite connection (via threading.local) to avoid
corruption from concurrent access.  WAL mode handles reader/writer
serialization at the database level.
"""

from __future__ import annotations

import functools
import json
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from platformdirs import user_data_dir

_DB_PATH = Path(user_data_dir("v8-utils")) / "cache.db"
_local = threading.local()
_init_lock = threading.Lock()
_schema_ready = False

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    user TEXT NOT NULL,
    status TEXT NOT NULL,
    created TEXT NOT NULL,
    patch_project TEXT,
    patch_change TEXT,
    patch_patchset TEXT,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_user_created ON jobs(user, created);
CREATE INDEX IF NOT EXISTS idx_jobs_patch ON jobs(patch_change);

CREATE TABLE IF NOT EXISTS results (
    job_id TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'histogram',
    data TEXT NOT NULL,
    PRIMARY KEY (job_id, source)
);

CREATE TABLE IF NOT EXISTS watermarks (
    user TEXT PRIMARY KEY,
    ceiling TEXT NOT NULL,
    floor TEXT NOT NULL
);
"""


_SCHEMA_VERSION = 3  # bump when schema changes to clear stale caches


def get_db() -> sqlite3.Connection:
    """Return a per-thread DB connection, creating it on first call per thread."""
    global _schema_ready
    conn: sqlite3.Connection | None = getattr(_local, "conn", None)
    if conn is not None:
        return conn
    # One-time schema setup (first thread wins)
    if not _schema_ready:
        with _init_lock:
            if not _schema_ready:
                _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
                _ensure_schema_version()
                init_conn = sqlite3.connect(str(_DB_PATH), timeout=10)
                init_conn.execute("PRAGMA journal_mode=WAL")
                init_conn.executescript(_SCHEMA)
                init_conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
                init_conn.close()
                _schema_ready = True
    conn = sqlite3.connect(str(_DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    _local.conn = conn
    return conn


def close_db() -> None:
    """Close the current thread's connection, if any."""
    conn: sqlite3.Connection | None = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


def _handle_corruption() -> None:
    """Delete the corrupt DB and reset state for recreation."""
    global _schema_ready
    print(
        f"Warning: cache database is corrupt — deleting and recreating.\n"
        f"  If this recurs, manually remove: {_DB_PATH}",
        file=sys.stderr,
    )
    # Close this thread's connection
    conn: sqlite3.Connection | None = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _local.conn = None
    with _init_lock:
        _DB_PATH.unlink(missing_ok=True)
        # Also remove WAL/SHM sidecar files
        _DB_PATH.with_suffix(".db-wal").unlink(missing_ok=True)
        _DB_PATH.with_suffix(".db-shm").unlink(missing_ok=True)
        _schema_ready = False


def _with_retry(fn):
    """Retry once after corruption recovery."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except sqlite3.DatabaseError:
            _handle_corruption()
            return fn(*args, **kwargs)

    return wrapper


def _ensure_schema_version() -> None:
    """Delete the cache file if its schema is outdated."""
    if not _DB_PATH.exists():
        return
    try:
        conn = sqlite3.connect(str(_DB_PATH))
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        if version != _SCHEMA_VERSION:
            _DB_PATH.unlink()
    except Exception:
        _DB_PATH.unlink(missing_ok=True)


# ── Patch URL parsing ─────────────────────────────────────────────────────────


def parse_patch_fields(
    url: str | None,
) -> tuple[str | None, str | None, str | None]:
    """Extract (project, change_id, patchset) from a Gerrit patch URL.

    Handles all forms:
      https://chromium-review.googlesource.com/c/v8/v8/+/7207174/14
      https://chromium-review.googlesource.com/7207174/14
      https://crrev.com/c/7207174/14
      7207174/14
      7207174

    Returns (None, None, None) if the URL cannot be parsed.
    """
    if not url:
        return None, None, None
    url = url.strip()
    parsed = urlparse(url)

    if parsed.scheme in ("http", "https"):
        host = parsed.hostname or ""
        path = parsed.path

        if "chromium-review.googlesource.com" in host or (
            "chromium-review" in host and "corp.google.com" in host
        ):
            plus_idx = path.find("/+/")
            if plus_idx != -1:
                # Canonical: /c/PROJECT/+/CHANGE[/PATCHSET]
                project_seg = path[:plus_idx].lstrip("/")
                if project_seg.startswith("c/"):
                    project_seg = project_seg[2:]
                project = project_seg.strip("/") or None
                change, patchset = _parse_change_patchset(path[plus_idx + 3 :])
                return project, change, patchset
            # Short: /CHANGE[/PATCHSET]
            change, patchset = _parse_change_patchset(path)
            return None, change, patchset

        if "crrev.com" in host:
            seg = "/" + path[3:] if path.startswith("/c/") else path
            change, patchset = _parse_change_patchset(seg)
            return None, change, patchset

        return None, None, None

    # No scheme: bare change ID or CHANGE/PATCHSET
    change, patchset = _parse_change_patchset(url.lstrip("/"))
    return None, change, patchset


def _parse_change_patchset(path: str) -> tuple[str | None, str | None]:
    """Extract (change_id, patchset) from a path like CHANGE[/PATCHSET]."""
    parts = [p for p in path.strip("/").split("/") if p]
    if parts and parts[0].isdigit():
        patchset = parts[1] if len(parts) > 1 and parts[1].isdigit() else None
        return parts[0], patchset
    return None, None


# ── Jobs ──────────────────────────────────────────────────────────────────────


def _job_fields(job: dict) -> tuple:
    """Extract indexed fields from a raw API job dict."""
    args = job.get("arguments", {})
    patch_url = args.get("experiment_patch")
    project, change, patchset = parse_patch_fields(patch_url)
    return (
        job.get("job_id"),
        job.get("user", ""),
        job.get("status", ""),
        job.get("created", ""),
        project,
        change,
        patchset,
        json.dumps(job),
    )


@_with_retry
def get_job(job_id: str) -> dict | None:
    """Look up a single job by ID. Returns the raw API dict or None."""
    row = (
        get_db().execute("SELECT data FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    )
    return json.loads(row[0]) if row else None


@_with_retry
def put_job(job: dict) -> None:
    """Insert or update a single job."""
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        _job_fields(job),
    )
    db.commit()


@_with_retry
def put_jobs(jobs: list[dict]) -> None:
    """Bulk insert/update jobs."""
    db = get_db()
    db.executemany(
        "INSERT OR REPLACE INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [_job_fields(j) for j in jobs if j.get("job_id")],
    )
    db.commit()


@_with_retry
def query_jobs(
    *,
    users: list[str] | None = None,
    since: str | None = None,
    change: str | None = None,
    patchset: str | None = None,
    status: str | None = None,
    exclude_statuses: list[str] | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Query cached jobs with optional filters. Returns raw API dicts."""
    clauses: list[str] = []
    params: list[str] = []
    if users:
        placeholders = ",".join("?" * len(users))
        clauses.append(f"user IN ({placeholders})")
        params.extend(users)
    if since:
        clauses.append("created >= ?")
        params.append(since)
    if change:
        clauses.append("patch_change = ?")
        params.append(change)
    if patchset:
        clauses.append("patch_patchset = ?")
        params.append(patchset)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if exclude_statuses:
        placeholders = ",".join("?" * len(exclude_statuses))
        clauses.append(f"status NOT IN ({placeholders})")
        params.extend(exclude_statuses)
    where = " AND ".join(clauses) if clauses else "1"
    sql = f"SELECT data FROM jobs WHERE {where} ORDER BY created DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    rows = get_db().execute(sql, params).fetchall()
    return [json.loads(r[0]) for r in rows]


# ── Results ───────────────────────────────────────────────────────────────────


@_with_retry
def get_results(job_id: str, source: str = "histogram") -> list[dict] | None:
    """Look up cached pivot_results for a job. Returns None on miss.

    source: "histogram" (default pivot_results) or "cas" (pivot_results_cas).
    """
    row = (
        get_db()
        .execute(
            "SELECT data FROM results WHERE job_id = ? AND source = ?",
            (job_id, source),
        )
        .fetchone()
    )
    return json.loads(row[0]) if row else None


@_with_retry
def put_results(job_id: str, rows: list[dict], source: str = "histogram") -> None:
    """Cache pivot_results rows for a completed job."""
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO results VALUES (?, ?, ?)",
        (job_id, source, json.dumps(rows)),
    )
    db.commit()


# ── Watermarks ────────────────────────────────────────────────────────────────


@_with_retry
def get_range(user: str) -> tuple[str | None, str | None]:
    """Return (ceiling, floor) for a user, or (None, None) if uncached."""
    row = (
        get_db()
        .execute("SELECT ceiling, floor FROM watermarks WHERE user = ?", (user,))
        .fetchone()
    )
    return (row[0], row[1]) if row else (None, None)


@_with_retry
def set_range(user: str, ceiling: str, floor: str) -> None:
    """Update the cached [floor, ceiling] range for a user."""
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO watermarks VALUES (?, ?, ?)",
        (user, ceiling, floor),
    )
    db.commit()


# ── Maintenance ───────────────────────────────────────────────────────────────


@_with_retry
def prune(days: int = 90) -> None:
    """Remove jobs and results older than `days`, updating floor accordingly."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    db = get_db()
    db.execute(
        "DELETE FROM results WHERE job_id IN "
        "(SELECT job_id FROM jobs WHERE created < ?)",
        (cutoff,),
    )
    db.execute("DELETE FROM jobs WHERE created < ?", (cutoff,))
    # Remove watermarks for users with no remaining jobs
    db.execute(
        "DELETE FROM watermarks WHERE user NOT IN (SELECT DISTINCT user FROM jobs)"
    )
    # Update floor to oldest remaining job per user
    db.execute(
        "UPDATE watermarks SET floor = "
        "(SELECT MIN(created) FROM jobs WHERE jobs.user = watermarks.user)"
    )
    db.commit()
