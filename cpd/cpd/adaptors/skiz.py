"""Adaptor template for skiz (Postgres/DuckDB) data.

Copy to ~/.config/cpd/adaptors/skiz.py and adjust if needed.

Config example:
    [sources.skiz]
    adaptor = "skiz"
    db_url = "postgres://user:pass@host/skiz"
"""

from __future__ import annotations

from collections.abc import Iterator
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

    def list_series(self, **filters: str) -> Iterator[SeriesKey]:
        conditions = ["submetric = ''"]
        params = []
        for col in ("bot", "benchmark", "test", "variant"):
            if col in filters:
                conditions.append(f"{col} = ?")
                params.append(filters[col])

        where = " AND ".join(conditions)
        df = _query(
            self._con,
            self._dialect,
            f"SELECT DISTINCT bot, benchmark, test, variant"
            f" FROM {_AGG_TABLE} WHERE {where}"
            f" ORDER BY bot, benchmark, test, variant",
            params,
        )

        for _, row in df.iterrows():
            yield SeriesKey(
                source="skiz",
                benchmark=f"{row['benchmark']} {row['test']}",
                metric=row["variant"],
                dimensions={
                    "bot": row["bot"],
                    "benchmark": row["benchmark"],
                    "test": row["test"],
                    "variant": row["variant"],
                },
            )

    def fetch_series(self, key: SeriesKey) -> list[SeriesPoint]:
        d = key.dimensions
        df = _query(
            self._con,
            self._dialect,
            f"SELECT commit_number, git_hash, commit_time,"
            f"       mean, stdev, count"
            f" FROM {_AGG_TABLE}"
            f" WHERE bot = ? AND benchmark = ? AND test = ?"
            f"   AND variant = ? AND submetric = ''"
            f" ORDER BY commit_number",
            [d["bot"], d["benchmark"], d["test"], d["variant"]],
        )

        return [
            SeriesPoint(
                commit_id=int(r["commit_number"]),
                mean=float(r["mean"]),
                stdev=float(r["stdev"] if pd.notna(r["stdev"]) else 0.0),
                count=int(r["count"]),
                commit_hash=r.get("git_hash"),
                commit_date=str(r["commit_time"])
                if pd.notna(r.get("commit_time"))
                else None,
            )
            for _, r in df.iterrows()
        ]


def create(**kwargs) -> SkizAdaptor:
    return SkizAdaptor(**kwargs)
