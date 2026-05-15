"""Adaptor for skiz (Spanner) perf data.

Config example:
    [sources.skiz]
    adaptor  = "skiz"
    project  = "v8-infra"
    instance = "v8-perf"
    database = "v8-perf"

Or with a single URL:
    [sources.skiz]
    adaptor = "skiz"
    url     = "spanner://v8-infra/v8-perf/v8-perf"

Requires Application Default Credentials:
    gcloud auth application-default login
"""

from __future__ import annotations

from urllib.parse import urlparse

import pandas as pd

_AGG_TABLE = "benchmarks"

# Variant prefixes that name an engine, per skiz/ingest._make_variant.
# A variant of "v8", "v8 (turbolev)", "jsc", "jsc (foo)" maps engine accordingly;
# anything else (e.g. raw flag-only variants like "default") leaves engine unset.
_KNOWN_ENGINES = {"v8", "jsc", "chromium"}


def _connect(project: str, instance: str, database: str):
    import os
    import warnings

    # Disable the built-in metrics exporter; it tries to push to Cloud
    # Monitoring and spews PERMISSION_DENIED tracebacks for ADC users that
    # don't have monitoring.timeSeries.create. Must be set before Client is
    # constructed (Client reads the env var in __init__).
    # google-cloud-spanner < 3.50 used SPANNER_ENABLE_BUILTIN_METRICS=false.
    os.environ.setdefault("SPANNER_DISABLE_BUILTIN_METRICS", "true")

    # google-auth ADC fires a quota-project UserWarning on every connection.
    warnings.filterwarnings(
        "ignore",
        message="Your application has authenticated using end user credentials",
        category=UserWarning,
        module=r"google\.auth\._default",
    )

    try:
        from google.cloud.spanner_dbapi import connect as dbapi_connect
    except ImportError as e:
        raise ImportError(
            "google-cloud-spanner required: uv add google-cloud-spanner"
        ) from e

    con = dbapi_connect(instance, database, project=project)
    con.autocommit = True

    # Validate creds with a cheap query. Without this the first failure is a
    # DDL call whose default retry policy retries auth errors for 3600 s.
    try:
        with con.cursor() as cur:
            cur.execute("SELECT 1")
    except Exception as e:
        con.close()
        raise ConnectionError(f"Spanner auth check failed: {e}") from e

    return con


def _query(con, sql: str, params: list) -> pd.DataFrame:
    with con.cursor() as cur:
        cur.execute(sql.replace("?", "%s"), params or None)
        cols = [desc[0] for desc in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols)


def _engine_from_variant(variant: str) -> str:
    """Extract engine name from skiz variant label (see skiz/ingest._make_variant).

    Empty string when the variant does not name a known engine; pandas groupby
    drops NaN groups by default, so we use "" as a stable sentinel.
    """
    if not variant:
        return ""
    head = variant.split(" ", 1)[0]
    return head if head in _KNOWN_ENGINES else ""


def _parse_url(url: str) -> tuple[str, str, str]:
    parsed = urlparse(url)
    if parsed.scheme != "spanner":
        raise ValueError(f"Expected spanner:// URL, got {url!r}")
    parts = parsed.path.strip("/").split("/")
    if len(parts) != 2 or not parsed.netloc:
        raise ValueError(
            f"Invalid spanner URL: {url!r} (expected spanner://project/instance/database)"
        )
    return parsed.netloc, parts[0], parts[1]


class SkizAdaptor:
    def __init__(
        self,
        project: str | None = None,
        instance: str | None = None,
        database: str | None = None,
        url: str | None = None,
        **_kwargs,
    ):
        if url is not None:
            project, instance, database = _parse_url(url)
        if not (project and instance and database):
            raise ValueError(
                "skiz adaptor requires either url=spanner://... or "
                "project/instance/database keys"
            )
        self._con = _connect(project, instance, database)

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
            f"SELECT bot, benchmark, test, variant,"
            f"       commit_number AS commit_id, git_hash, commit_time,"
            f"       mean AS value, stdev, count"
            f" FROM {_AGG_TABLE}"
            f" WHERE {where}"
            f" ORDER BY bot, benchmark, test, variant, commit_number",
            params,
        )
        if not df.empty:
            df["engine"] = df["variant"].map(_engine_from_variant)
        return df


def create(**kwargs) -> SkizAdaptor:
    return SkizAdaptor(**kwargs)
