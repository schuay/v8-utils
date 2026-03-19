"""Adaptor template for slipstream SQLite data.

Copy to ~/.config/cpd/adaptors/slipstream.py and adjust if needed.

Config example:
    [sources.slipstream]
    adaptor = "slipstream"
    db_path = "~/src/slipstream/metadata/slipstream.db"
    engine = "v8"
"""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path

from cpd.models import SeriesKey, SeriesPoint


class SlipstreamAdaptor:
    def __init__(self, db_path: str, engine: str = "v8", **_kwargs):
        self._db = Path(db_path).expanduser()
        self._engine = engine
        self._conn = sqlite3.connect(str(self._db))
        self._conn.row_factory = sqlite3.Row

    def list_series(self, **filters: str) -> Iterator[SeriesKey]:
        rows = self._conn.execute(
            "SELECT DISTINCT suite, flags, benchmark, metric FROM scores"
            " WHERE engine=? ORDER BY suite, flags, benchmark, metric",
            (self._engine,),
        ).fetchall()

        for row in rows:
            suite, flags, benchmark, metric = row
            yield SeriesKey(
                source="slipstream",
                benchmark=f"{suite}[{flags}] {benchmark}",
                metric=metric,
                dimensions={
                    "engine": self._engine,
                    "suite": suite,
                    "flags": flags,
                    "benchmark": benchmark,
                },
            )

    def fetch_series(
        self,
        key: SeriesKey,
        since: str | None = None,
        until: str | None = None,
    ) -> list[SeriesPoint]:
        d = key.dimensions

        # Join with commits to filter by date
        where = (
            "s.engine=? AND s.suite=? AND s.flags=? AND s.benchmark=? AND s.metric=?"
        )
        params: list = [
            self._engine,
            d["suite"],
            d["flags"],
            d["benchmark"],
            key.metric,
        ]

        if since:
            where += " AND c.date >= ?"
            params.append(since)
        if until:
            where += " AND c.date <= ?"
            params.append(until)

        score_rows = self._conn.execute(
            "SELECT s.commit_id, s.score FROM scores s"
            " JOIN commits c ON s.engine = c.engine AND s.commit_id = c.commit_id"
            f" WHERE {where}"
            " ORDER BY s.commit_id",
            params,
        ).fetchall()

        # Group raw scores by commit, compute aggregates
        by_commit: dict[int, list[float]] = defaultdict(list)
        for r in score_rows:
            by_commit[r["commit_id"]].append(r["score"])

        # Fetch commit metadata for the same date range
        meta_where = "engine=? AND commit_id IS NOT NULL"
        meta_params: list = [self._engine]
        if since:
            meta_where += " AND date >= ?"
            meta_params.append(since)
        if until:
            meta_where += " AND date <= ?"
            meta_params.append(until)

        commit_info = {}
        for r in self._conn.execute(
            f"SELECT commit_id, hash, date, title FROM commits WHERE {meta_where}",
            meta_params,
        ).fetchall():
            commit_info[r["commit_id"]] = r

        points = []
        for cid in sorted(by_commit):
            scores = by_commit[cid]
            n = len(scores)
            m = sum(scores) / n
            s = math.sqrt(sum((x - m) ** 2 for x in scores) / (n - 1)) if n > 1 else 0.0
            info = commit_info.get(cid)
            points.append(
                SeriesPoint(
                    commit_id=cid,
                    mean=m,
                    stdev=s,
                    count=n,
                    commit_hash=info["hash"] if info else None,
                    commit_date=info["date"] if info else None,
                    commit_title=info["title"] if info else None,
                )
            )

        return points


def create(**kwargs) -> SlipstreamAdaptor:
    return SlipstreamAdaptor(**kwargs)
