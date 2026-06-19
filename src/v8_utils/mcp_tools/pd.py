"""MCP tools for pd — perf data analysis (change-point detection and AB compare)."""

import io
from fnmatch import fnmatch

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from rich.console import Console

from .. import config as v8_config
from ..pd import report
from ..pd.adaptor import discover
from ..pd.at import at_from_df
from ..pd.commits import CommitStore
from ..pd.compare import compare_snapshots
from ..pd.detect import detect_from_df
from ..pd.models import AnalysisConfig, AtConfig
from ._shared import _text_result


def _load_config() -> v8_config.Config:
    return v8_config.load()


def _make_adaptor(source: str, cfg: v8_config.Config):
    sources = cfg.sources
    if source not in sources:
        available = ", ".join(sorted(sources)) or "(none configured)"
        raise ValueError(f"Unknown source {source!r}. Available: {available}")
    source_cfg = dict(sources[source])
    adaptor_name = source_cfg.pop("adaptor", source)
    adaptors = discover()
    if adaptor_name not in adaptors:
        raise ValueError(
            f"Adaptor {adaptor_name!r} not found. "
            f"Available: {', '.join(sorted(adaptors))}"
        )
    return adaptors[adaptor_name](**source_cfg)


def _engine_for_source(source: str, cfg: v8_config.Config) -> str | None:
    return cfg.sources.get(source, {}).get("engine")


def _parse_date(value: str) -> str:
    """Parse '2026-01-15' or 'two weeks ago' into a YYYY-MM-DD string."""
    import dateparser

    dt = dateparser.parse(value, settings={"PREFER_DATES_FROM": "past"})
    if dt is None:
        raise ValueError(f"Cannot parse date: {value!r}")
    return dt.strftime("%Y-%m-%d")


def _render(fn, *args, **kwargs) -> str:
    """Run a pd.report print function, capturing its rich output as plain text."""
    buf = io.StringIO()
    old = report.console
    report.console = Console(
        file=buf, width=120, force_terminal=False, color_system=None, highlight=False
    )
    try:
        fn(*args, **kwargs)
    finally:
        report.console = old
    return buf.getvalue().rstrip() or "(no output)"


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def pd_detect(
        benchmark: str | None = None,
        engine: str | None = None,
        bot: str = "mac-m3-jgruber",
        since: str = "two weeks ago",
        until: str | None = None,
        metric: str | None = None,
        min_change: float = 3.0,
        group: bool = True,
        penalty: float | None = None,
        min_effect: float | None = None,
        source: str = "skiz",
    ) -> CallToolResult:
        """Detect change points (regressions/improvements) in benchmark time series.

        Runs PELT change-point detection per series and reports significant
        shifts with percent change, effect size, p-value, and the commit range
        each shift falls into.

        Output size grows with the number of series scanned. Omitting
        `benchmark` scans every benchmark and can produce a very large report;
        prefer a `benchmark` (and optionally `metric`) filter, raise
        `min_change`, or shorten the `since` window to bound the result.

        benchmark:  benchmark name filter, e.g. "jetstream3.slipstream" or
                    "jetstream2.slipstream". Omit to scan all benchmarks (large).
        engine:     engine filter, "v8" or "jsc". Omit to include all engines.
        bot:        bot name filter (default "mac-m3-jgruber").
        since:      only include commits after this date. Plain-text dates work,
                    e.g. "two weeks ago" (default) or "2026-05-01".
        until:      only include commits before this date (optional).
        metric:     metric/test glob filter, e.g. "Total*" (optional).
        min_change: minimum percent change to report (default 3 = 3%).
        group:      group results by commit (default True).
        penalty:    PELT penalty override (optional; defaults from config).
        min_effect: minimum Cohen's d override (optional; defaults from config).
        source:     data source name (default "skiz").
        """
        cfg = _load_config()
        since_date = _parse_date(since) if since else None
        until_date = _parse_date(until) if until else None

        analysis_cfg = cfg.analysis
        config = AnalysisConfig(
            penalty=penalty or analysis_cfg.get("penalty", 3.0),
            min_effect_size=min_effect or analysis_cfg.get("min_effect_size", 0.5),
            min_pct_change=min_change or analysis_cfg.get("min_pct_change", 1.0),
        )

        filter_kwargs: dict[str, str] = {}
        if bot:
            filter_kwargs["bot"] = bot
        if benchmark:
            filter_kwargs["benchmark"] = benchmark

        adaptor = _make_adaptor(source, cfg)
        default_engine = _engine_for_source(source, cfg)
        commit_store = CommitStore()
        try:
            fetched = adaptor.fetch(since=since_date, until=until_date, **filter_kwargs)

            if metric:
                fetched = fetched[fetched["test"].apply(lambda t: fnmatch(t, metric))]

            if engine:
                if "engine" not in fetched.columns:
                    raise ValueError(
                        f"source {source!r} does not expose an engine column"
                    )
                fetched = fetched[fetched["engine"] == engine]

            results = detect_from_df(fetched, config)
            text = _render(
                report.print_detect_report,
                results,
                group_by_commit=group,
                commit_store=commit_store,
                default_engine=default_engine,
            )
        finally:
            commit_store.close()

        return _text_result(text)

    @mcp.tool()
    def pd_at(
        commit: str,
        benchmark: str | None = None,
        variant: str | None = None,
        engine: str | None = None,
        bot: str = "mac-m3-jgruber",
        metric: str | None = None,
        history: int = 20,
        min_change: float = 3.0,
        show_all: bool = False,
        source: str = "skiz",
    ) -> CallToolResult:
        """Assess what changed at a specific commit (before vs after).

        For a known, usually very recent commit, compares the measurements just
        before it against those at/after it, per series. The noise scale is
        estimated from the surrounding history (robust lag-1 differences), so
        the verdict holds even with only a point or two after the commit, where
        change-point detection (pd_detect) cannot work. The target is snapped to
        the nearest measured commit >= it.

        Each row reports before/after levels, percent change, SNR (step over the
        series' own noise), a z-score and FDR-adjusted significance, the
        before/after sample counts, a confidence tag, and a sparkline of the
        surrounding series with the commit marked. A `*` on the confidence tag
        means the commit is the newest measured point, so the change is
        unconfirmed and may be transient (re-run as more data lands).

        Typically narrow to one engine/variant/benchmark (and optionally
        metric); without filters this assesses every series around the commit.

        commit:     target commit position (numeric id) or git hash prefix.
        benchmark:  benchmark name filter, e.g. "jetstream3.slipstream".
        variant:    variant filter, e.g. "default" or "turbolev".
        engine:     engine filter, "v8" or "jsc".
        bot:        bot name filter (default "mac-m3-jgruber").
        metric:     metric/test glob filter, e.g. "Total*" (optional).
        history:    commits of history for the noise estimate (default 20).
        min_change: minimum percent change to flag (default 3 = 3%).
        show_all:   include below-threshold series (default False).
        source:     data source name (default "skiz").
        """
        cfg = _load_config()
        adaptor = _make_adaptor(source, cfg)
        commit_engine = _engine_for_source(source, cfg)
        store = CommitStore()
        try:
            if commit.isdigit():
                target_id = int(commit)
                info = store.get(commit_engine, target_id) if commit_engine else None
            elif commit_engine:
                info = store.get_by_hash(commit_engine, commit)
                if info is None:
                    raise ValueError(
                        f"commit {commit!r} not found for engine {commit_engine!r}"
                        " (run `pd sync` to populate commit metadata)"
                    )
                target_id = info.id
            else:
                raise ValueError(
                    f"commit {commit!r} is a hash but source {source!r} has no engine"
                    " for lookup; pass a numeric commit position instead"
                )

            since_date = _parse_date("6 months ago")
            if info and info.date:
                from datetime import datetime, timedelta

                try:
                    base = datetime.strptime(info.date, "%Y-%m-%d")
                    since_date = (base - timedelta(days=90)).strftime("%Y-%m-%d")
                except ValueError:
                    pass

            config = AtConfig(history=history, min_pct_change=min_change)

            filter_kwargs: dict[str, str] = {}
            if bot:
                filter_kwargs["bot"] = bot
            if benchmark:
                filter_kwargs["benchmark"] = benchmark

            fetched = adaptor.fetch(since=since_date, until=None, **filter_kwargs)

            if metric:
                fetched = fetched[fetched["test"].apply(lambda t: fnmatch(t, metric))]
            if variant:
                fetched = fetched[fetched["variant"] == variant]
            if engine:
                if "engine" not in fetched.columns:
                    raise ValueError(
                        f"source {source!r} does not expose an engine column"
                    )
                fetched = fetched[fetched["engine"] == engine]

            deltas = at_from_df(fetched, target_id, config)
        finally:
            store.close()

        header = [f"At commit {commit} (snap >= {target_id})"]
        filt = " ".join(
            f"{k}={v}"
            for k, v in {
                "bot": bot,
                "benchmark": benchmark,
                "variant": variant,
                "engine": engine,
                "metric": metric,
            }.items()
            if v
        )
        if filt:
            header.append(filt)

        snapped = deltas[0].snapped_commit_id if deltas else target_id
        text = _render(report.print_at_report, deltas, snapped, header, show_all)
        return _text_result(text)

    @mcp.tool()
    def pd_compare(
        a: list[str],
        b: list[str],
        benchmark: str | None = None,
        bot: str = "mac-m3-jgruber",
        since: str = "two weeks ago",
        until: str | None = None,
        show_all: bool = False,
        alpha: float = 0.05,
        source: str = "skiz",
    ) -> CallToolResult:
        """Compare two configurations (A vs B) of benchmark data.

        Each side is defined by field=value overrides on the dimension columns
        (bot, benchmark, test, variant). Filters not overridden on either side
        become the join keys, and the report shows per-key A vs B means, percent
        change, and FDR-corrected significance.

        a:          A-side (base) overrides, e.g. ["variant=default"].
        b:          B-side (experiment) overrides, e.g. ["variant=turbolev"].
        benchmark:  benchmark filter applied to both sides, e.g.
                    "jetstream3.slipstream" or "jetstream2.slipstream" (optional).
        bot:        bot filter applied to both sides (default "mac-m3-jgruber").
        since:      only include commits after this date. Plain-text dates work,
                    e.g. "two weeks ago" (default) or "2026-05-01".
        until:      only include commits before this date (optional).
        show_all:   include non-significant results (default False).
        alpha:      significance threshold after FDR correction (default 0.05).
        source:     data source name (default "skiz").
        """
        cfg = _load_config()
        since_date = _parse_date(since) if since else None
        until_date = _parse_date(until) if until else None

        def _parse_overrides(items: list[str]) -> dict[str, str]:
            result: dict[str, str] = {}
            for item in items:
                if "=" not in item:
                    raise ValueError(f"override must be key=value, got: {item!r}")
                k, v = item.split("=", 1)
                result[k] = v
            return result

        a_overrides = _parse_overrides(a)
        b_overrides = _parse_overrides(b)

        common: dict[str, str] = {}
        if bot:
            common["bot"] = bot
        if benchmark:
            common["benchmark"] = benchmark

        adaptor = _make_adaptor(source, cfg)
        filters_a = {**common, **a_overrides}
        filters_b = {**common, **b_overrides}
        df_a = adaptor.fetch(since=since_date, until=until_date, **filters_a)
        df_b = adaptor.fetch(since=since_date, until=until_date, **filters_b)

        all_override_keys = set(a_overrides) | set(b_overrides)
        dimension_cols = ["bot", "benchmark", "test", "variant"]
        key_cols = [c for c in dimension_cols if c not in all_override_keys]

        result_df = compare_snapshots(df_a, df_b, key_cols, alpha=alpha)

        a_desc = " ".join(f"{k}={v}" for k, v in a_overrides.items())
        b_desc = " ".join(f"{k}={v}" for k, v in b_overrides.items())
        common_desc = " ".join(f"{k}={v}" for k, v in common.items())
        header = [f"A: {a_desc}  B: {b_desc}"]
        if common_desc:
            header.append(f"common: {common_desc}")

        text = _render(
            report.print_compare_report,
            result_df,
            key_cols,
            header,
            show_all=show_all,
        )
        return _text_result(text)
