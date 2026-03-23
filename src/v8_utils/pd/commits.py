"""Lightweight SQLite commit metadata store.

Populated from engine git repos, provides commit titles and ranges
for change-point reports.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
from pathlib import Path

from platformdirs import user_config_dir

from .models import CommitInfo

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS commits (
    engine    TEXT NOT NULL,
    hash      TEXT NOT NULL,
    commit_id INTEGER,
    date      TEXT NOT NULL DEFAULT '',
    timestamp INTEGER NOT NULL DEFAULT 0,
    title     TEXT NOT NULL DEFAULT '',
    author    TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (engine, hash)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_commit_id
    ON commits (engine, commit_id) WHERE commit_id IS NOT NULL;
"""

_MIGRATE_AUTHOR = """\
ALTER TABLE commits ADD COLUMN author TEXT NOT NULL DEFAULT '';
"""

_DEFAULT_PATH = Path(user_config_dir("v8-utils")) / "commits.db"


class CommitStore:
    def __init__(self, db_path: Path | None = None):
        if db_path is None:
            db_path = _DEFAULT_PATH
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        try:
            self.conn.execute(_MIGRATE_AUTHOR)
        except sqlite3.OperationalError:
            pass

    def get(self, engine: str, commit_id: int) -> CommitInfo | None:
        row = self.conn.execute(
            "SELECT commit_id, hash, date, timestamp, title, author FROM commits"
            " WHERE engine=? AND commit_id=?",
            (engine, commit_id),
        ).fetchone()
        if not row:
            return None
        return CommitInfo(
            id=row["commit_id"],
            hash=row["hash"],
            date=row["date"],
            timestamp=row["timestamp"],
            title=row["title"],
            author=row["author"],
        )

    def get_by_hash(self, engine: str, hash_prefix: str) -> CommitInfo | None:
        """Look up a commit by hash prefix."""
        row = self.conn.execute(
            "SELECT commit_id, hash, date, timestamp, title, author FROM commits"
            " WHERE engine=? AND hash LIKE ?",
            (engine, hash_prefix + "%"),
        ).fetchone()
        if not row:
            return None
        return CommitInfo(
            id=row["commit_id"],
            hash=row["hash"],
            date=row["date"],
            timestamp=row["timestamp"],
            title=row["title"],
            author=row["author"],
        )

    def get_range(self, engine: str, after_id: int, up_to_id: int) -> list[CommitInfo]:
        """All commits with after_id < commit_id <= up_to_id."""
        rows = self.conn.execute(
            "SELECT commit_id, hash, date, timestamp, title, author FROM commits"
            " WHERE engine=? AND commit_id IS NOT NULL"
            " AND commit_id > ? AND commit_id <= ?"
            " ORDER BY commit_id",
            (engine, after_id, up_to_id),
        ).fetchall()
        return [
            CommitInfo(
                id=r["commit_id"],
                hash=r["hash"],
                date=r["date"],
                timestamp=r["timestamp"],
                title=r["title"],
                author=r["author"],
            )
            for r in rows
        ]

    def populate(
        self,
        engine: str,
        src_dir: Path,
        id_regex: str,
        since: str | None = None,
    ) -> int:
        """Populate commit metadata from git log."""
        git_format = "%H|%cs|%ct|%ae|%s|%b%n--END-COMMIT--"
        cmd = f'git log origin/main --pretty=format:"{git_format}"'
        if since:
            cmd += f' --since="{since}"'

        res = subprocess.run(
            cmd, shell=True, cwd=src_dir, capture_output=True, text=True
        )
        if res.returncode != 0:
            return 0

        count = 0
        for raw in res.stdout.strip().split("--END-COMMIT--"):
            raw = raw.strip()
            if not raw:
                continue
            parts = raw.split("|", 5)
            if len(parts) < 6:
                continue
            h, date_str, ts, author, subject, body = parts

            matches = re.findall(id_regex, subject + "\n" + body, re.MULTILINE)
            if not matches:
                continue

            commit_id = int(matches[-1])
            title = subject.replace('"', "")

            self.conn.execute(
                "INSERT OR REPLACE INTO commits"
                " (engine, hash, commit_id, date, timestamp, title, author)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (engine, h, commit_id, date_str, int(ts), title, author),
            )
            count += 1

        self.conn.commit()
        return count

    def close(self):
        self.conn.close()
