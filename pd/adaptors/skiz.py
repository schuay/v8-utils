"""Adaptor for skiz (Postgres/DuckDB) perf data.

Copy to ~/.config/v8-utils/adaptors/skiz.py and adjust if needed.

Config example:
    [sources.skiz]
    adaptor = "skiz"
    db_url = "postgres://user:pass@host/skiz"
"""

from __future__ import annotations

from urllib.parse import urlparse

import pandas as pd

_AGG_TABLE = "agg.benchmarks"


def _connect(url: str):
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
    ) -> pd.DataFrame:
        """Fetch all matching data as a flat DataFrame."""
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
            from datetime import date, timedelta

            conditions.append("commit_time < ?")
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
            f"       commit_number AS commit_id, git_hash, commit_time,"
            f"       mean AS value, stdev, count"
            f" FROM {_AGG_TABLE}"
            f" WHERE {where}"
            f" ORDER BY bot, benchmark, test, variant, commit_number",
            params,
        )
        return df


def create(**kwargs) -> SkizAdaptor:
    return SkizAdaptor(**kwargs)
