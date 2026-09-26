"""MCP tools for pd -- perf data analysis (change-point detection and AB compare).

Argument schemas and rendering only; the operations are v8_utils.pd.api.
"""

import io
import logging
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from pydantic import Field
from rich.console import Console

from ..pd import api, report
from ..pd.commits import CommitStore
from ..pd.serialize import changepoints_to_payload
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
                    '"text" for a human report, or "json" for a paged envelope'
                    " of structured change points with candidate probabilities"
                    " and resolved git hashes, for programmatic consumers"
                )
            ),
        ] = "text",
        min_localization_confidence: Annotated[
            float,
            Field(
                description=(
                    "drop points whose breakpoint is localized to less than this"
                    " probability (0 keeps all); use it when only well-localized"
                    " points are actionable"
                )
            ),
        ] = 0.0,
        limit: Annotated[
            int,
            Field(
                description=(
                    "json only: max change points per page, ordered by"
                    " localization confidence then magnitude"
                )
            ),
        ] = 100,
        offset: Annotated[
            int, Field(description="json only: page offset into that order")
        ] = 0,
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

        format="json" returns {changepoints, total, offset, returned, truncated}.
        A truncated page means points remain: advance offset by returned to read
        the next one. Ordering is a stable total order, so pages do not overlap.
        min_localization_confidence gates on how sharply a point is pinned to one
        commit, and applies to both formats.

        """
        store = CommitStore()
        try:
            det = api.detect(
                source,
                store=store,
                bot=bot,
                benchmark=benchmark,
                metric=metric,
                engine=engine,
                since=api.parse_date(since) if since else None,
                until=api.parse_date(until) if until else None,
                penalty=penalty,
                min_effect=min_effect,
                min_change=min_change,
                min_localization_confidence=min_localization_confidence,
            )
            if format == "json":
                import json

                out = json.dumps(
                    changepoints_to_payload(
                        det.points, store, det.default_engine, limit, offset
                    )
                )
            else:
                out = _render(
                    report.print_detect_report,
                    det.points,
                    group_by_commit=group,
                    commit_store=store,
                    default_engine=det.default_engine,
                )
        finally:
            store.close()
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
        store = CommitStore()
        try:
            impact = api.commit_impact(
                source,
                commit,
                store=store,
                bot=bot,
                benchmark=benchmark,
                variant=variant,
                metric=metric,
                engine=engine,
                history=history,
                min_change=min_change,
            )
        finally:
            store.close()
        text = _render(
            report.print_at_report,
            impact.deltas,
            impact.snapped_commit_id,
            impact.header,
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
        cmp = api.compare(
            source,
            a,
            b,
            bot=bot,
            benchmark=benchmark,
            since=api.parse_date(since) if since else None,
            until=api.parse_date(until) if until else None,
            alpha=alpha,
        )
        text = _render(
            report.print_compare_report,
            cmp.table,
            cmp.key_cols,
            cmp.header,
            show_all=show_all,
        )
        return _text_result(text)
