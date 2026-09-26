"""The pd operations, as functions. Reached through v8_utils.api.pd.

A frontend parses arguments and renders results. Everything between -- source
lookup, fetch, filtering, detection, commit resolution -- is here, once. The
CLI and the MCP tool used to compose these steps by hand and had drifted (only
the tool gated on localization and auto-synced commits); a daemon that wants
the same answer in-process then had to call the tool over MCP and page around
a character cap meant for model context.

Errors are ValueError with a message a caller can show as-is. The frontends
translate: the CLI to an exit code, the tool to a tool error.

The CommitStore is the caller's: it is needed both during detection (commit
auto-sync) and while rendering (hash resolution), so its lifetime brackets
both and cannot be internal here.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from fnmatch import fnmatch
from typing import TYPE_CHECKING, Any

from .. import config as v8_config
from .adaptor import check_dimension_values, discover
from .at import at_from_df
from .commits import CommitStore
from .compare import compare_snapshots
from .detect import detect_from_df
from .engines import sync_engine
from .models import AnalysisConfig, AtConfig, ChangePoint, CommitDelta
from .serialize import ResolvedChangePoint, filter_by_localization, resolve_changepoint

if TYPE_CHECKING:
    import pandas as pd

log = logging.getLogger(__name__)

# The dimension columns a compare joins on unless a side overrides them.
DIMENSION_COLUMNS = ("bot", "benchmark", "test", "variant")

# Per-engine ceiling: the highest change-point id this process has already
# attempted a sync for. Anti-thrash guard -- a change point can reference an id
# the local checkout never reaches (the source runs ahead of our origin/main
# mirror, or fetch is failing), and since detection is stateless that same
# unresolved point recurs on every poll. Without this, each recurrence re-runs
# a full fetch + full-window git-log populate, piling that cost onto every poll
# exactly when the remote is flaky. Keyed on the attempted id (not the store
# max, which never reaches an unreachable id), so a stuck point syncs once and
# is then suppressed until a genuinely higher point appears -- whose sync also
# pulls in the older one if it has since become reachable. Process-local: a
# long-lived server keeps it across polls, and a restart resets it, which is
# the right moment to retry anyway.
_SYNC_CEILING: dict[str, int] = {}


def load_config() -> v8_config.Config:
    """The v8-utils config. A seam for tests and for callers that hold one."""
    return v8_config.load()


def parse_date(value: str) -> str:
    """'2026-01-15' or 'two weeks ago' as YYYY-MM-DD."""
    import dateparser

    dt = dateparser.parse(value, settings={"PREFER_DATES_FROM": "past"})
    if dt is None:
        raise ValueError(f"Cannot parse date: {value!r}")
    return dt.strftime("%Y-%m-%d")


def make_adaptor(source: str, cfg: v8_config.Config):
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


def engine_for_source(source: str, cfg: v8_config.Config) -> str | None:
    return cfg.sources.get(source, {}).get("engine")


def parse_overrides(items: list[str]) -> dict[str, str]:
    """`field=value` items as a dict."""
    out: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"override must be key=value, got: {item!r}")
        k, v = item.split("=", 1)
        out[k] = v
    return out


def _dimension_filters(bot: str | None, benchmark: str | None) -> dict[str, str]:
    filters: dict[str, str] = {}
    if bot:
        filters["bot"] = bot
    if benchmark:
        filters["benchmark"] = benchmark
    return filters


def _fetch(adaptor, since, until, filters: dict[str, str]) -> pd.DataFrame:
    t0 = time.monotonic()
    fetched = adaptor.fetch(since=since, until=until, **filters)
    log.info("fetch: %d rows in %.1fs", len(fetched), time.monotonic() - t0)
    if fetched.empty:
        check_dimension_values(adaptor, filters)
    return fetched


def _narrow(
    fetched: pd.DataFrame,
    *,
    source: str,
    adaptor,
    metric: str | None,
    engine: str | None,
    variant: str | None = None,
) -> pd.DataFrame:
    """The post-fetch filters a source cannot apply itself."""
    if metric:
        fetched = fetched[fetched["test"].apply(lambda t: fnmatch(t, metric))]
        log.info("after metric filter: %d rows", len(fetched))
    if variant:
        if fetched.empty or variant not in set(fetched["variant"]):
            check_dimension_values(adaptor, {"variant": variant})
        fetched = fetched[fetched["variant"] == variant]
    if engine:
        if "engine" not in fetched.columns:
            raise ValueError(f"source {source!r} does not expose an engine column")
        fetched = fetched[fetched["engine"] == engine]
        log.info("after engine filter: %d rows", len(fetched))
    return fetched


def autosync_commits(
    commit_store: CommitStore, results: list[ChangePoint], default_engine: str | None
) -> None:
    """Refresh the commit store when a change point outruns it.

    A change point's commit_id resolves to a git hash via the store; a point
    past the store's newest synced commit resolves to "" and a consumer that
    needs the hash drops it as unlocalized. That happens whenever new commits
    have landed since the last sync -- which, without this, only a manual
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


@dataclass
class Detection:
    points: list[ChangePoint]
    #: The source's engine, for resolving a point that carries none of its own.
    default_engine: str | None


def detect(
    source: str,
    *,
    store: CommitStore,
    bot: str | None = None,
    benchmark: str | None = None,
    metric: str | None = None,
    engine: str | None = None,
    since: str | None = None,
    until: str | None = None,
    penalty: float | None = None,
    min_effect: float | None = None,
    min_change: float | None = None,
    min_localization_confidence: float = 0.0,
    autosync: bool = True,
    cfg: v8_config.Config | None = None,
) -> Detection:
    """Change points in every series of `source` that the filters select.

    `since`/`until` are already parsed dates (see parse_date). `metric` is a
    glob over the test column. Tuning left None comes from the config's
    [analysis] table. `min_localization_confidence` drops points whose
    breakpoint is not pinned to one commit at least that sharply; `autosync`
    refreshes the commit store when a point outruns it.
    """
    cfg = cfg or load_config()
    analysis = cfg.analysis
    config = AnalysisConfig(
        penalty=penalty or analysis.get("penalty", 3.0),
        min_effect_size=min_effect or analysis.get("min_effect_size", 0.5),
        min_pct_change=min_change or analysis.get("min_pct_change", 1.0),
    )
    adaptor = make_adaptor(source, cfg)
    default_engine = engine_for_source(source, cfg)
    fetched = _fetch(adaptor, since, until, _dimension_filters(bot, benchmark))
    fetched = _narrow(
        fetched, source=source, adaptor=adaptor, metric=metric, engine=engine
    )
    t0 = time.monotonic()
    points = detect_from_df(fetched, config)
    log.info("detect: %d change points in %.1fs", len(points), time.monotonic() - t0)
    points = filter_by_localization(points, min_localization_confidence)
    if autosync:
        autosync_commits(store, points, default_engine)
    return Detection(points=points, default_engine=default_engine)


def detect_resolved(source: str, **kwargs) -> list[ResolvedChangePoint]:
    """detect(), with every point's commits resolved and no store to manage.

    Takes detect()'s keyword arguments except `store`. The commit store is
    opened, auto-synced and closed here, so a caller that only needs the
    points never handles it.
    """
    store = CommitStore()
    try:
        det = detect(source, store=store, **kwargs)
        return [resolve_changepoint(cp, store, det.default_engine) for cp in det.points]
    finally:
        store.close()


def resolve_commit(
    store: CommitStore, engine: str | None, commit: str
) -> tuple[int, str | None]:
    """A commit argument (position or hash prefix) as (commit_id, date)."""
    if commit.isdigit():
        cid = int(commit)
        info = store.get(engine, cid) if engine else None
        return cid, (info.date if info else None)
    if not engine:
        raise ValueError(
            f"commit {commit!r} is a hash but the source has no engine for lookup;"
            " pass a numeric commit position instead"
        )
    info = store.get_by_hash(engine, commit)
    if info is None:
        raise ValueError(
            f"commit {commit!r} not found for engine {engine!r}"
            " (run `pd sync` to populate commit metadata)"
        )
    return info.id, info.date


@dataclass
class Impact:
    deltas: list[CommitDelta]
    target_id: int
    target_date: str | None
    #: The measured commit the assessment snapped to (target_id when no data).
    snapped_commit_id: int
    header: list[str] = field(default_factory=list)


def commit_impact(
    source: str,
    commit: str,
    *,
    store: CommitStore,
    bot: str | None = None,
    benchmark: str | None = None,
    variant: str | None = None,
    metric: str | None = None,
    engine: str | None = None,
    history: int = 20,
    min_change: float | None = None,
    min_z: float | None = None,
    since: str | None = None,
    cfg: v8_config.Config | None = None,
) -> Impact:
    """Before/after assessment of every selected series at `commit`.

    The fetch window defaults to 90 days before the commit when its date is
    known, else six months back; `since` (already parsed) overrides it.
    """
    cfg = cfg or load_config()
    adaptor = make_adaptor(source, cfg)
    commit_engine = engine_for_source(source, cfg)
    target_id, target_date = resolve_commit(store, commit_engine, commit)
    if since is None:
        since = parse_date("6 months ago")
        if target_date:
            try:
                base = datetime.strptime(target_date, "%Y-%m-%d")
                since = (base - timedelta(days=90)).strftime("%Y-%m-%d")
            except ValueError:
                pass
    config = AtConfig(
        history=history,
        min_pct_change=min_change or cfg.analysis.get("min_pct_change", 1.0),
        min_z=min_z if min_z is not None else AtConfig.min_z,
    )
    fetched = _fetch(adaptor, since, None, _dimension_filters(bot, benchmark))
    fetched = _narrow(
        fetched,
        source=source,
        adaptor=adaptor,
        metric=metric,
        engine=engine,
        variant=variant,
    )
    deltas = at_from_df(fetched, target_id, config)
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
    return Impact(
        deltas=deltas,
        target_id=target_id,
        target_date=target_date,
        snapped_commit_id=deltas[0].snapped_commit_id if deltas else target_id,
        header=header,
    )


@dataclass
class Comparison:
    table: Any  # pandas DataFrame, one row per join key
    key_cols: list[str]
    a: dict[str, str]
    b: dict[str, str]
    common: dict[str, str]
    header: list[str] = field(default_factory=list)


def compare(
    source: str,
    a: list[str],
    b: list[str],
    *,
    bot: str | None = None,
    benchmark: str | None = None,
    since: str | None = None,
    until: str | None = None,
    alpha: float = 0.05,
    cfg: v8_config.Config | None = None,
) -> Comparison:
    """A versus B over the window, joined on every dimension neither side
    overrides. `a` and `b` are `field=value` items."""
    cfg = cfg or load_config()
    a_overrides = parse_overrides(a)
    b_overrides = parse_overrides(b)
    common = _dimension_filters(bot, benchmark)
    adaptor = make_adaptor(source, cfg)
    filters_a = {**common, **a_overrides}
    filters_b = {**common, **b_overrides}
    df_a = _fetch(adaptor, since, until, filters_a)
    df_b = _fetch(adaptor, since, until, filters_b)
    overridden = set(a_overrides) | set(b_overrides)
    key_cols = [c for c in DIMENSION_COLUMNS if c not in overridden]
    t0 = time.monotonic()
    table = compare_snapshots(df_a, df_b, key_cols, alpha=alpha)
    log.info("compare: %d rows in %.1fs", len(table), time.monotonic() - t0)
    a_desc = " ".join(f"{k}={v}" for k, v in a_overrides.items())
    b_desc = " ".join(f"{k}={v}" for k, v in b_overrides.items())
    header = [f"A: {a_desc}  B: {b_desc}"]
    if common:
        header.append("common: " + " ".join(f"{k}={v}" for k, v in common.items()))
    return Comparison(
        table=table,
        key_cols=key_cols,
        a=a_overrides,
        b=b_overrides,
        common=common,
        header=header,
    )
