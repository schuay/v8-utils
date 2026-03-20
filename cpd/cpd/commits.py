"""Lightweight SQLite commit metadata store.

Populated from engine git repos, provides commit titles and ranges
for change-point reports.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
from pathlib import Path

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

_DEFAULT_PATH = Path("~/.config/cpd/commits.db").expanduser()


class CommitStore:
    def __init__(self, db_path: Path | None = None):
        path = db_path or _DEFAULT_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        # Migrate: add author column if missing
        try:
            self.conn.execute(_MIGRATE_AUTHOR)
        except sqlite3.OperationalError:
            pass  # column already exists

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

    def date_for_id(self, engine: str, commit_id: int) -> str | None:
        row = self.conn.execute(
            "SELECT date FROM commits WHERE engine=? AND commit_id=?",
            (engine, commit_id),
        ).fetchone()
        return row["date"] if row else None

    def max_commit_id(self, engine: str) -> int | None:
        row = self.conn.execute(
            "SELECT MAX(commit_id) FROM commits WHERE engine=?",
            (engine,),
        ).fetchone()
        return row[0] if row and row[0] is not None else None

    def populate(
        self,
        engine: str,
        src_dir: Path,
        id_regex: str,
        since: str | None = None,
    ) -> int:
        """Populate commit metadata from git log.

        Args:
            engine: Engine name (e.g. "v8", "jsc").
            src_dir: Path to the engine's git repo.
            id_regex: Regex to extract commit ID from commit message.
            since: Optional date string for --since flag to git log.

        Returns:
            Number of commits inserted/updated.
        """
        # Fetch latest commits from remote
        subprocess.run(
            "git fetch origin main",
            shell=True,
            cwd=src_dir,
            capture_output=True,
        )

        git_format = "%H|%cs|%ct|%an|%s|%b%n--END-COMMIT--"
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

            # Use the last match: reverts/relands copy the original message,
            # so the commit's own ID always appears last.
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
