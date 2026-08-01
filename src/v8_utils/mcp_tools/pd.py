"""MCP tools for pd — perf data analysis (change-point detection and AB compare)."""

import io
import logging
from fnmatch import fnmatch

from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from pydantic import Field
from rich.console import Console

from .. import config as v8_config
from ..pd import report
from ..pd.adaptor import check_dimension_values, discover
from ..pd.at import at_from_df
from ..pd.commits import CommitStore
from ..pd.compare import compare_snapshots
from ..pd.detect import detect_from_df
from ..pd.engines import sync_engine
from ..pd.models import AnalysisConfig, AtConfig
from ._shared import _text_result

# Argument documentation lives on the argument (Annotated[..., Field(...)]) so a
# client sends it as the parameter's own schema description rather than leaving
# the model to match a prose line against a signature by name.
BENCHMARK_ARG = 'benchmark filter, e.g. "jetstream3.slipstream"'
BOT_ARG = "bot to filter on"
SOURCE_ARG = "data source"
SINCE_ARG = "window start; natural language is accepted"
METRIC_ARG = 'test glob, e.g. "Total*"'
MIN_CHANGE_ARG = "minimum percent change to report"

log = logging.getLogger(__name__)

# Per-engine ceiling: the highest change-point id this process has already
# attempted a sync for. Anti-thrash guard -- a change point can reference an id
# the local checkout never reaches (skiz runs ahead of our origin/main mirror,
# or fetch is failing), and since detection is stateless that same unresolved
# point recurs on every poll. Without this, each recurrence re-runs a full fetch
# + full-window git-log populate, piling that cost onto every poll exactly when
# the remote is flaky. Keyed on the attempted id (not the store max, which never
# reaches an unreachable id), so a stuck point syncs once and is then suppressed
# until a genuinely higher point appears -- whose sync also pulls in the older
# one if it has since become reachable. Process-local: the MCP server is
# long-lived across polls, and a restart resets it, which is the right moment to
# retry anyway.
_SYNC_CEILING: dict[str, int] = {}


def _autosync_commits(commit_store, results, default_engine) -> None:
    """Refresh the commit store when a change point outruns it.

    pd_detect resolves each change point's commit_id to a git hash via the store;
    a point past the store's newest synced commit resolves to "" and the consumer
    (airc's perf subscriber) drops it as unlocalized. That happens whenever new
    commits have landed since the last sync -- which, without this, only a manual
    `pd sync` fixed. Sync the affected engine once, in place, so the very call
    that surfaced the gap can resolve the hash.

    Two gates decide whether to sync a given engine, both required:
    - the top referenced id exceeds the store's current max (there is a gap), and
    - it also exceeds this process's sync ceiling (we have not already tried, and
      failed, to close a gap this large -- see _SYNC_CEILING).
    Best-effort: any failure leaves the store as-is and the point renders
    unlocalized, exactly as before.
    """
    # Group the highest referenced id per engine; a point's own engine wins, else
    # the source's default (mirrors serialize's engine resolution).
    needed: dict[str, int] = {}
    for cp in results:
        engine = cp.engine or default_engine
        if not engine:
            continue
        top = max(cp.commit_id, cp.prev_commit_id)
        needed[engine] = max(needed.get(engine, 0), top)

    for engine, top_id in needed.items():
        have = commit_store.max_commit_id(engine)
        if have is not None and top_id <= have:
            continue  # already resolvable; no gap
        if top_id <= _SYNC_CEILING.get(engine, 0):
            continue  # already attempted this gap (or larger) and it did not help
        # Record the attempt before syncing, so a raised/timed-out sync still
        # suppresses the retry storm rather than re-firing next poll.
        _SYNC_CEILING[engine] = top_id
        log.info(
            "pd: auto-sync %s: change point at %d exceeds stored max %s",
            engine,
            top_id,
            have,
        )
        try:
            n = sync_engine(commit_store, engine)
            log.info("pd: auto-sync %s: %d commits processed", engine, n)
        except Exception:
            log.warning("pd: auto-sync %s failed", engine, exc_info=True)


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
        benchmark: Annotated[
            str | None,
            Field(description=f"{BENCHMARK_ARG}; omit to scan all (large)"),
        ] = None,
        engine: Annotated[str | None, Field(description='"v8" or "jsc"')] = None,
        bot: Annotated[str, Field(description=BOT_ARG)] = "mac-m3-jgruber",
        since: Annotated[str, Field(description=SINCE_ARG)] = "two weeks ago",
        until: Annotated[
            str | None, Field(description="window end; natural language ok")
        ] = None,
        metric: Annotated[str | None, Field(description=METRIC_ARG)] = None,
        min_change: Annotated[float, Field(description=MIN_CHANGE_ARG)] = 3.0,
        group: Annotated[
            bool, Field(description="group related series in the report")
        ] = True,
        penalty: Annotated[
            float | None,
            Field(description="PELT penalty override (default: from config)"),
        ] = None,
        min_effect: Annotated[
            float | None,
            Field(description="minimum effect size override (default: from config)"),
        ] = None,
        source: Annotated[str, Field(description=SOURCE_ARG)] = "skiz",
        format: Annotated[
            str,
            Field(
                description=(
                    '"text" for a human report, or "json" for structured change'
                    " points with candidate probabilities and resolved git"
                    " hashes, for programmatic consumers"
                )
            ),
        ] = "text",
    ) -> CallToolResult:
        """Scan benchmark timelines for regressions/improvements (change points).

        Use when you do NOT know where a change happened: PELT searches each
        series over a date range and reports significant shifts with the commit
        range each falls in. Needs several points around a shift, so for a known
        or very recent commit use pd_commit_impact instead; to compare two fixed
        configurations use pd_compare.

        Narrow with benchmark/engine/metric; omitting benchmark scans all series
        (large). Shows only significant shifts. An unknown bot/benchmark value is
        rejected with the list of valid names, so a wrong guess is self-correcting.

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
            if fetched.empty:
                check_dimension_values(adaptor, filter_kwargs)

            if metric:
                fetched = fetched[fetched["test"].apply(lambda t: fnmatch(t, metric))]

            if engine:
                if "engine" not in fetched.columns:
                    raise ValueError(
                        f"source {source!r} does not expose an engine column"
                    )
                fetched = fetched[fetched["engine"] == engine]

            results = detect_from_df(fetched, config)
            _autosync_commits(commit_store, results, default_engine)
            if format == "json":
                import json

                from ..pd.serialize import changepoints_to_json

                out = json.dumps(
                    changepoints_to_json(results, commit_store, default_engine)
                )
            else:
                out = _render(
                    report.print_detect_report,
                    results,
                    group_by_commit=group,
                    commit_store=commit_store,
                    default_engine=default_engine,
                )
        finally:
            commit_store.close()

        return _text_result(out, stale_banner=format != "json")

    @mcp.tool()
    def pd_commit_impact(
        commit: Annotated[
            str, Field(description="commit position (numeric id) or git hash prefix")
        ],
        benchmark: Annotated[str | None, Field(description=BENCHMARK_ARG)] = None,
        variant: Annotated[
            str | None,
            Field(description='e.g. "v8_default"; encodes the engine'),
        ] = None,
        engine: Annotated[str | None, Field(description='"v8" or "jsc"')] = None,
        bot: Annotated[str, Field(description=BOT_ARG)] = "mac-m3-jgruber",
        metric: Annotated[str | None, Field(description=METRIC_ARG)] = None,
        history: Annotated[
            int, Field(description="commits of history for the noise estimate")
        ] = 20,
        min_change: Annotated[
            float, Field(description="minimum percent change to flag")
        ] = 3.0,
        show_all: Annotated[
            bool, Field(description="include below-threshold series")
        ] = False,
        chart_only: Annotated[
            bool,
            Field(description="emit one sparkline line per series, no table"),
        ] = False,
        source: Annotated[str, Field(description=SOURCE_ARG)] = "skiz",
    ) -> CallToolResult:
        """Assess one specific commit's impact on benchmarks: before vs after.

        Use when you have a concrete commit (position or hash) and want its
        effect, especially recent commits where pd_detect has too little
        post-commit data to work. Compares measurements just before the commit
        against those at/after it per series; the noise scale comes from the
        surrounding history, so the verdict holds with only a point or two after.
        Snaps to the nearest measured commit >= the target. To find unknown
        change points over time use pd_detect; for two fixed configs, pd_compare.

        Each row: before->after level, percent change, SNR (step over the
        series' own noise), FDR significance, n_before/n_after, a confidence tag,
        and a sparkline of the surround with the commit marked. A `*` on the tag
        means the commit is the newest measured point, so the change is
        unconfirmed and may be transient. Significant-only unless show_all.

        Narrow with engine/variant/benchmark/metric; variant encodes the engine
        (e.g. "v8_default", "v8_turbolev_future"), so it pins the engine on its
        own. An unknown bot/benchmark/variant is rejected with the valid names.

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

            if fetched.empty:
                check_dimension_values(adaptor, filter_kwargs)

            if metric:
                fetched = fetched[fetched["test"].apply(lambda t: fnmatch(t, metric))]
            if variant:
                if fetched.empty or variant not in set(fetched["variant"]):
                    check_dimension_values(adaptor, {"variant": variant})
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
        text = _render(
            report.print_at_report,
            deltas,
            snapped,
            header,
            show_all,
            chart_only,
            show_levels=False,
        )
        return _text_result(text)

    @mcp.tool()
    def pd_compare(
        a: Annotated[
            list[str],
            Field(description='A-side (base) overrides, e.g. ["variant=v8_default"]'),
        ],
        b: Annotated[
            list[str],
            Field(
                description=('B-side overrides, e.g. ["variant=v8_turbolev_future"]')
            ),
        ],
        benchmark: Annotated[
            str | None, Field(description=f"{BENCHMARK_ARG}; filters both sides")
        ] = None,
        bot: Annotated[
            str, Field(description=f"{BOT_ARG}; filters both sides")
        ] = "mac-m3-jgruber",
        since: Annotated[str, Field(description=SINCE_ARG)] = "two weeks ago",
        until: Annotated[
            str | None, Field(description="window end; natural language ok")
        ] = None,
        show_all: Annotated[
            bool, Field(description="include non-significant results")
        ] = False,
        alpha: Annotated[float, Field(description="FDR significance level")] = 0.05,
        source: Annotated[str, Field(description=SOURCE_ARG)] = "skiz",
    ) -> CallToolResult:
        """Compare two fixed benchmark configurations head to head (A vs B).

        Use when you have two configs to compare directly (variants, bots,
        flag-sets), not a timeline. Each side is field=value overrides on
        bot/benchmark/test/variant; dimensions left unoverridden become the join
        keys. Reports per-key A vs B means, percent change, and FDR-corrected
        significance. To find changes over time use pd_detect; for one specific
        commit's effect, pd_commit_impact. An unknown bot/benchmark/variant on
        either side is rejected with the list of valid names.

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
        if df_a.empty:
            check_dimension_values(adaptor, filters_a)
        if df_b.empty:
            check_dimension_values(adaptor, filters_b)

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
