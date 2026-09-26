"""pd.api: the operations under the CLI and the MCP tools, driven with an
in-memory adaptor."""

from __future__ import annotations

import pandas as pd
import pytest

from v8_utils.pd import api
from v8_utils.pd.commits import CommitStore

COLUMNS = [
    "bot",
    "benchmark",
    "test",
    "variant",
    "commit_id",
    "value",
    "stdev",
    "count",
    "commit_time",
    "git_hash",
]


def _step_rows(test: str, variant: str = "default", before=100.0, after=105.0):
    """A series with one clear step between commit 1009 and 1010."""
    rows = []
    for i, cid in enumerate(range(1000, 1020)):
        level = before if i < 10 else after
        rows.append(
            ["bot1", "bench", test, variant, cid, level, 0.2, 3, "2026-01-01", "h"]
        )
    return rows


class FakeAdaptor:
    def __init__(self, rows, engine_column: bool = False):
        self.df = pd.DataFrame(rows, columns=COLUMNS)
        if engine_column:
            self.df["engine"] = "v8"
        self.fetch_calls: list[dict] = []

    def fetch(self, since=None, until=None, **filters):
        self.fetch_calls.append({"since": since, "until": until, **filters})
        df = self.df
        for k, v in filters.items():
            df = df[df[k] == v]
        return df

    def distinct_values(self, column):
        return sorted(set(self.df[column]))


class FakeConfig:
    def __init__(self, adaptor, engine: str | None = "v8"):
        self.adaptor = adaptor
        self.sources = {"fake": {"adaptor": "fake", "engine": engine}}
        self.analysis = {}


@pytest.fixture
def cfg(monkeypatch):
    def _build(rows, engine="v8", engine_column=False):
        adaptor = FakeAdaptor(rows, engine_column=engine_column)
        c = FakeConfig(adaptor, engine=engine)
        monkeypatch.setattr(api, "discover", lambda: {"fake": lambda **kw: adaptor})
        return c

    return _build


@pytest.fixture
def store(tmp_path):
    s = CommitStore(tmp_path / "commits.db")
    yield s
    s.close()


def test_detect_finds_the_step_and_carries_the_source_engine(cfg, store):
    c = cfg(_step_rows("Total"))
    det = api.detect("fake", store=store, cfg=c, autosync=False)
    assert det.default_engine == "v8"
    assert len(det.points) == 1
    cp = det.points[0]
    assert cp.metric == "Total"
    assert cp.direction in ("improvement", "regression")
    assert (cp.prev_commit_id, cp.commit_id) == (1009, 1010)


def test_detect_passes_window_and_dimension_filters_to_the_source(cfg, store):
    c = cfg(_step_rows("Total"))
    api.detect(
        "fake",
        store=store,
        cfg=c,
        bot="bot1",
        benchmark="bench",
        since="2026-01-01",
        until="2026-02-01",
        autosync=False,
    )
    assert c.adaptor.fetch_calls == [
        {
            "since": "2026-01-01",
            "until": "2026-02-01",
            "bot": "bot1",
            "benchmark": "bench",
        }
    ]


def test_detect_metric_is_a_glob(cfg, store):
    c = cfg(_step_rows("Total") + _step_rows("Other"))
    det = api.detect("fake", store=store, cfg=c, metric="Tot*", autosync=False)
    assert [cp.metric for cp in det.points] == ["Total"]


def test_detect_engine_filter_needs_an_engine_column(cfg, store):
    c = cfg(_step_rows("Total"))
    with pytest.raises(ValueError, match="does not expose an engine column"):
        api.detect("fake", store=store, cfg=c, engine="v8", autosync=False)
    c = cfg(_step_rows("Total"), engine_column=True)
    assert (
        api.detect("fake", store=store, cfg=c, engine="jsc", autosync=False).points
        == []
    )
    assert (
        len(api.detect("fake", store=store, cfg=c, engine="v8", autosync=False).points)
        == 1
    )


def test_detect_localization_gate_is_applied(cfg, store):
    c = cfg(_step_rows("Total"))
    kept = api.detect("fake", store=store, cfg=c, autosync=False).points
    assert kept
    # No point is localized above certainty, so the gate drops every one.
    gated = api.detect(
        "fake", store=store, cfg=c, min_localization_confidence=1.01, autosync=False
    ).points
    assert gated == []


def test_detect_autosyncs_the_store_for_the_source_engine(cfg, store, monkeypatch):
    c = cfg(_step_rows("Total"))
    synced = []
    monkeypatch.setattr(
        api, "sync_engine", lambda s, engine: synced.append(engine) or 0
    )
    api._SYNC_CEILING.clear()
    api.detect("fake", store=store, cfg=c)
    assert synced == ["v8"]


def test_unknown_source_names_the_available_ones(cfg, store):
    c = cfg(_step_rows("Total"))
    with pytest.raises(ValueError, match="Unknown source 'nope'. Available: fake"):
        api.detect("nope", store=store, cfg=c)


def test_empty_fetch_reports_a_bad_dimension_value(cfg, store):
    c = cfg(_step_rows("Total"))
    with pytest.raises(ValueError, match="bot"):
        api.detect("fake", store=store, cfg=c, bot="no-such-bot", autosync=False)


def test_commit_impact_snaps_and_builds_the_header(cfg, store):
    c = cfg(_step_rows("Total"))
    impact = api.commit_impact(
        "fake",
        "1010",
        store=store,
        cfg=c,
        bot="bot1",
        min_change=3.0,
        since="2025-01-01",
    )
    assert impact.target_id == 1010
    assert impact.snapped_commit_id == 1010
    assert len(impact.deltas) == 1
    assert impact.header == ["At commit 1010 (snap >= 1010)", "bot=bot1"]
    assert c.adaptor.fetch_calls[0]["since"] == "2025-01-01"


def test_commit_impact_hash_needs_an_engine(cfg, store):
    c = cfg(_step_rows("Total"), engine=None)
    with pytest.raises(ValueError, match="pass a numeric commit position"):
        api.commit_impact("fake", "abc123", store=store, cfg=c)


def test_commit_impact_unknown_hash_is_an_error(cfg, store):
    c = cfg(_step_rows("Total"))
    with pytest.raises(ValueError, match="not found for engine 'v8'"):
        api.commit_impact("fake", "abc123", store=store, cfg=c)


def test_compare_joins_on_the_dimensions_neither_side_overrides(cfg, store):
    c = cfg(
        _step_rows("Total", "a", 100.0, 100.0) + _step_rows("Total", "b", 110.0, 110.0)
    )
    cmp = api.compare("fake", ["variant=a"], ["variant=b"], cfg=c, bot="bot1")
    assert cmp.key_cols == ["bot", "benchmark", "test"]
    assert cmp.header == ["A: variant=a  B: variant=b", "common: bot=bot1"]
    assert len(cmp.table) == 1


def test_compare_rejects_a_bare_override(cfg, store):
    c = cfg(_step_rows("Total"))
    with pytest.raises(ValueError, match="key=value"):
        api.compare("fake", ["variant"], ["variant=b"], cfg=c)


def test_parse_date_rejects_nonsense():
    with pytest.raises(ValueError, match="Cannot parse date"):
        api.parse_date("not a date at all zzz")
    assert api.parse_date("2026-01-15") == "2026-01-15"
