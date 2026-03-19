"""CLI for cpd — change-point detection on benchmark time series."""

from __future__ import annotations

import tomllib
from fnmatch import fnmatch
from typing import Annotated, Optional

import typer

from .adaptor import CONFIG_DIR, discover
from .detect import detect
from .models import AnalysisConfig
from .report import print_report

app = typer.Typer(help="PELT change-point detection for benchmark time series.")


def _load_config() -> dict:
    path = CONFIG_DIR / "config.toml"
    if path.exists():
        return tomllib.loads(path.read_text())
    return {}


def _make_adaptor(name: str, cfg: dict):
    """Instantiate an adaptor by name, using config for kwargs."""
    sources = cfg.get("sources", {})
    source_cfg = sources.get(name, {})
    adaptor_name = source_cfg.pop("adaptor", name)

    adaptors = discover()
    if adaptor_name not in adaptors:
        typer.echo(f"Error: adaptor '{adaptor_name}' not found.", err=True)
        typer.echo(f"Available: {', '.join(sorted(adaptors))}", err=True)
        raise typer.Exit(1)

    return adaptors[adaptor_name](**source_cfg)


@app.command("detect")
def detect_cmd(
    source: Annotated[str, typer.Argument(help="Data source name")],
    benchmark: Annotated[
        Optional[str], typer.Option("--benchmark", "-b", help="Benchmark glob filter")
    ] = None,
    metric: Annotated[
        Optional[str], typer.Option("--metric", "-m", help="Metric glob filter")
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

    # Build analysis config from defaults + config file + CLI overrides
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

    all_results = []
    series_data = {}

    for key in adaptor.list_series(**filter_kwargs):
        if benchmark and not fnmatch(key.benchmark, benchmark):
            continue
        if metric and not fnmatch(key.metric, metric):
            continue

        series = adaptor.fetch_series(key)
        if not series:
            continue

        results = detect(series, key=key, config=config)
        if results:
            all_results.extend(results)
            series_data[key] = series

    print_report(all_results, group_by_commit=group_by_commit, series_data=series_data)


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

[analysis]
penalty = 3.0
min_effect_size = 0.5
min_pct_change = 0.01
"""
    )
    typer.echo(f"Created {config_path}")
