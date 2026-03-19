"""CLI for cpd — change-point detection on benchmark time series."""

from __future__ import annotations

import tomllib
from fnmatch import fnmatch
from typing import Annotated, Optional

import typer

from .adaptor import CONFIG_DIR, discover
from .commits import CommitStore
from .detect import detect
from .engines import ENGINES, get_id_regex, get_src_dir
from .models import AnalysisConfig
from .report import print_report

app = typer.Typer(help="PELT change-point detection for benchmark time series.")


def _load_config() -> dict:
    path = CONFIG_DIR / "config.toml"
    if path.exists():
        return tomllib.loads(path.read_text())
    return {}


def _parse_date(value: str) -> str:
    """Parse a date string like '2026-01-15' or '2 weeks ago' into YYYY-MM-DD."""
    import dateparser

    dt = dateparser.parse(value)
    if dt is None:
        raise typer.BadParameter(f"Cannot parse date: {value!r}")
    return dt.strftime("%Y-%m-%d")


def _make_adaptor(name: str, cfg: dict):
    """Instantiate an adaptor by name, using config for kwargs."""
    sources = cfg.get("sources", {})
    source_cfg = dict(sources.get(name, {}))
    adaptor_name = source_cfg.pop("adaptor", name)

    adaptors = discover()
    if adaptor_name not in adaptors:
        typer.echo(f"Error: adaptor '{adaptor_name}' not found.", err=True)
        typer.echo(f"Available: {', '.join(sorted(adaptors))}", err=True)
        raise typer.Exit(1)

    return adaptors[adaptor_name](**source_cfg)


def _engine_for_source(name: str, cfg: dict) -> str | None:
    """Get the engine name associated with a source (from config or adaptor defaults)."""
    sources = cfg.get("sources", {})
    return sources.get(name, {}).get("engine")


@app.command("detect")
def detect_cmd(
    source: Annotated[str, typer.Argument(help="Data source name")],
    benchmark: Annotated[
        Optional[str], typer.Option("--benchmark", "-b", help="Benchmark glob filter")
    ] = None,
    metric: Annotated[
        Optional[str], typer.Option("--metric", "-m", help="Metric glob filter")
    ] = None,
    since: Annotated[
        Optional[str],
        typer.Option(
            help="Only include commits on or after this date (YYYY-MM-DD or '2 weeks ago')"
        ),
    ] = None,
    until: Annotated[
        Optional[str],
        typer.Option(
            help="Only include commits on or before this date (YYYY-MM-DD or 'yesterday')"
        ),
    ] = None,
    penalty: Annotated[
        Optional[float], typer.Option("--penalty", help="PELT penalty")
    ] = None,
    min_effect: Annotated[
        Optional[float], typer.Option("--min-effect", help="Min Cohen's d")
    ] = None,
    min_change: Annotated[
        Optional[float], typer.Option("--min-change", help="Min pct change")
    ] = None,
    group_by_commit: Annotated[
        bool, typer.Option("--group", help="Group results by commit")
    ] = False,
    filters: Annotated[
        Optional[list[str]],
        typer.Option("--filter", "-f", help="key=value filters passed to adaptor"),
    ] = None,
):
    """Detect change points in benchmark time series."""
    cfg = _load_config()

    # Parse dates
    since_date = _parse_date(since) if since else None
    until_date = _parse_date(until) if until else None

    # Build analysis config
    analysis_cfg = cfg.get("analysis", {})
    config = AnalysisConfig(
        penalty=penalty or analysis_cfg.get("penalty", 3.0),
        min_effect_size=min_effect or analysis_cfg.get("min_effect_size", 0.5),
        min_pct_change=min_change or analysis_cfg.get("min_pct_change", 0.01),
    )

    # Parse --filter key=value pairs
    filter_kwargs = {}
    for f in filters or []:
        if "=" not in f:
            typer.echo(f"Error: filter must be key=value, got: {f}", err=True)
            raise typer.Exit(1)
        k, v = f.split("=", 1)
        filter_kwargs[k] = v

    adaptor = _make_adaptor(source, cfg)

    # Open commit store for report
    engine = _engine_for_source(source, cfg)
    commit_store = CommitStore()

    all_results = []
    series_data = {}

    for key in adaptor.list_series(**filter_kwargs):
        if benchmark and not fnmatch(key.benchmark, benchmark):
            continue
        if metric and not fnmatch(key.metric, metric):
            continue

        series = adaptor.fetch_series(key, since=since_date, until=until_date)
        if not series:
            continue

        results = detect(series, key=key, config=config)
        if results:
            all_results.extend(results)
            series_data[key] = series

    print_report(
        all_results,
        group_by_commit=group_by_commit,
        series_data=series_data,
        commit_store=commit_store,
        engine=engine,
    )
    commit_store.close()


@app.command()
def sources():
    """List available data source adaptors."""
    adaptors = discover()
    if not adaptors:
        typer.echo("No adaptors found.")
        typer.echo("Place adaptor scripts in ~/.config/cpd/adaptors/")
        return

    for name in sorted(adaptors):
        typer.echo(f"  {name}")


@app.command()
def series(
    source: Annotated[str, typer.Argument(help="Data source name")],
    filters: Annotated[
        Optional[list[str]],
        typer.Option("--filter", "-f", help="key=value filters"),
    ] = None,
):
    """List available time series from a data source."""
    cfg = _load_config()

    filter_kwargs = {}
    for f in filters or []:
        if "=" not in f:
            typer.echo(f"Error: filter must be key=value, got: {f}", err=True)
            raise typer.Exit(1)
        k, v = f.split("=", 1)
        filter_kwargs[k] = v

    adaptor = _make_adaptor(source, cfg)
    count = 0
    for key in adaptor.list_series(**filter_kwargs):
        typer.echo(f"  {key.benchmark}  [{key.metric}]")
        count += 1
    typer.echo(f"\n{count} series found.")


@app.command()
def sync(
    engine: Annotated[str, typer.Argument(help="Engine to sync (v8, jsc)")],
    since: Annotated[
        Optional[str],
        typer.Option(help="Sync commits since this date (default: 6 months ago)"),
    ] = None,
):
    """Populate commit metadata from an engine's git repo."""
    id_regex = get_id_regex(engine)
    if not id_regex:
        typer.echo(f"Error: unknown engine '{engine}'", err=True)
        typer.echo(f"Available: {', '.join(sorted(ENGINES))}", err=True)
        raise typer.Exit(1)

    src_dir = get_src_dir(engine)
    if not src_dir or not src_dir.is_dir():
        typer.echo(
            f"Error: source directory for '{engine}' not found."
            f" Check v8-utils config (~/.config/v8-utils/config.toml).",
            err=True,
        )
        raise typer.Exit(1)

    since_date = since or "6 months ago"

    store = CommitStore()
    typer.echo(f"Syncing {engine} commits from {src_dir} (since {since_date})...")
    count = store.populate(engine, src_dir, id_regex, since=since_date)
    typer.echo(f"  {count} commits processed.")
    store.close()


@app.command()
def init():
    """Create config directory and template config file."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    (CONFIG_DIR / "adaptors").mkdir(exist_ok=True)

    config_path = CONFIG_DIR / "config.toml"
    if config_path.exists():
        typer.echo(f"Config already exists: {config_path}")
        return

    config_path.write_text(
        """\
# cpd configuration
# Data source definitions — each needs an adaptor name + connection params.

# [sources.slipstream]
# adaptor = "slipstream"
# db_path = "~/src/slipstream/metadata/slipstream.db"
# engine = "v8"

# [sources.skiz]
# adaptor = "skiz"
# db_url = "postgres://user:pass@host/skiz"
# engine = "v8"

[analysis]
penalty = 3.0
min_effect_size = 0.5
min_pct_change = 0.01
"""
    )
    typer.echo(f"Created {config_path}")
