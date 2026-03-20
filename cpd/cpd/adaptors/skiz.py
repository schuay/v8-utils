"""Adaptor template for skiz (Postgres/DuckDB) data.

Copy to ~/.config/cpd/adaptors/skiz.py and adjust if needed.

Config example:
    [sources.skiz]
    adaptor = "skiz"
    db_url = "postgres://user:pass@host/skiz"
"""

from __future__ import annotations

from urllib.parse import urlparse

import pandas as pd

from cpd.models import SeriesKey, SeriesPoint

_AGG_TABLE = "agg.benchmarks"


def _connect(url: str):
    """Connect to DuckDB or Postgres based on URL scheme."""
    parsed = urlparse(url)
    if parsed.scheme in ("postgres", "postgresql"):
        import psycopg2

        con = psycopg2.connect(url)
        con.autocommit = True
        return con, "postgres"
    else:
        import duckdb

        return duckdb.connect(url, read_only=True), "duckdb"


def _query(con, dialect: str, sql: str, params: list) -> pd.DataFrame:
    if dialect == "postgres":
        with con.cursor() as cur:
            cur.execute(sql.replace("?", "%s"), params or None)
            cols = [desc[0] for desc in cur.description]
            return pd.DataFrame(cur.fetchall(), columns=cols)
    else:
        return con.execute(sql, params).df()


class SkizAdaptor:
    def __init__(self, db_url: str, **_kwargs):
        self._con, self._dialect = _connect(db_url)

    def fetch(
        self,
        since: str | None = None,
        until: str | None = None,
        **filters: str,
    ) -> dict[SeriesKey, list[SeriesPoint]]:
        """Fetch all series matching filters in a single query."""
        conditions = ["submetric = ''"]
        params: list = []

        filter_map = {
            "bot": "bot",
            "benchmark": "benchmark",
            "test": "test",
            "variant": "variant",
        }
        for key, col in filter_map.items():
            if key in filters:
                conditions.append(f"{col} = ?")
                params.append(filters[key])

        if since:
            conditions.append("commit_time >= ?")
            params.append(since)
        if until:
            conditions.append("commit_time < ?")
            from datetime import date, timedelta

            try:
                dt = date.fromisoformat(until) + timedelta(days=1)
                params.append(dt.isoformat())
            except ValueError:
                conditions[-1] = "commit_time <= ?"
                params.append(until)

        where = " AND ".join(conditions)
        df = _query(
            self._con,
            self._dialect,
            f"SELECT bot, benchmark, test, variant,"
            f"       commit_number, git_hash, commit_time,"
            f"       mean, stdev, count"
            f" FROM {_AGG_TABLE}"
            f" WHERE {where}"
            f" ORDER BY bot, benchmark, test, variant, commit_number",
            params,
        )

        result: dict[SeriesKey, list[SeriesPoint]] = {}
        for _, r in df.iterrows():
            key = SeriesKey(
                source="skiz",
                benchmark=r["benchmark"],
                metric=r["test"],
                dimensions={
                    "bot": r["bot"],
                    "benchmark": r["benchmark"],
                    "test": r["test"],
                    "variant": r["variant"],
                },
            )
            point = SeriesPoint(
                commit_id=int(r["commit_number"]),
                mean=float(r["mean"]),
                stdev=float(r["stdev"] if pd.notna(r["stdev"]) else 0.0),
                count=int(r["count"]),
                commit_hash=r.get("git_hash"),
                commit_date=str(r["commit_time"])
                if pd.notna(r.get("commit_time"))
                else None,
            )
            result.setdefault(key, []).append(point)

        return result


def create(**kwargs) -> SkizAdaptor:
    return SkizAdaptor(**kwargs)
