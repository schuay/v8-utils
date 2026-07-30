import asyncio
import types

import pytest
from mcp.server.fastmcp import FastMCP

import v8_utils.gerrit as g
from v8_utils.mcp_tools import gerrit as mcp_gerrit


def test_open_cls_extracts_revision_and_fetch_ref(monkeypatch):
    fake = [
        {
            "_number": 123,
            "project": "v8/v8",
            "subject": "Fix the thing",
            "owner": {"email": "alice@google.com", "_account_id": 42},
            "current_revision": "deadbeef",
            "revisions": {"deadbeef": {"ref": "refs/changes/23/123/2"}},
        }
    ]
    monkeypatch.setattr(g, "_resolve_self", lambda q: q)
    monkeypatch.setattr(g, "_get", lambda host, path: fake)
    out = g.open_cls("project:v8/v8 status:open")
    assert out == [
        {
            "number": 123,
            "project": "v8/v8",
            "subject": "Fix the thing",
            "owner": "alice@google.com",
            "revision": "deadbeef",
            "fetch_ref": "refs/changes/23/123/2",
        }
    ]


def test_open_cls_requests_current_revision(monkeypatch):
    seen = {}
    monkeypatch.setattr(g, "_resolve_self", lambda q: q)
    monkeypatch.setattr(g, "_get", lambda host, path: seen.update(path=path) or [])
    g.open_cls("status:open")
    assert (
        "o=CURRENT_REVISION" in seen["path"] and "o=DETAILED_ACCOUNTS" in seen["path"]
    )


def test_open_cls_tolerates_missing_fields(monkeypatch):
    monkeypatch.setattr(g, "_resolve_self", lambda q: q)
    monkeypatch.setattr(g, "_get", lambda host, path: [{"_number": 5, "project": "p"}])
    (row,) = g.open_cls("x")
    assert row["revision"] == "" and row["fetch_ref"] == "" and row["owner"] == ""


@pytest.fixture
def cq_spec(monkeypatch):
    """Register gerrit_cq and capture the -cl spec it passes to bb."""
    seen = {}

    def fake_bb_run(args, timeout=60):
        seen["spec"] = args[args.index("-cl") + 1]
        return types.SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(mcp_gerrit, "_bb_run", fake_bb_run)
    server = FastMCP("test")
    mcp_gerrit.register(server)

    def call(change, patchset=1):
        asyncio.run(
            server.call_tool("gerrit_cq", {"change": change, "patchset": patchset})
        )
        return seen["spec"]

    return call


# bb matches CLs on host, change number and patchset only, so gerrit_cq omits
# the project segment; hardcoding v8/v8 would read as a v8-only tool.
class TestGerritCqClSpec:
    def test_bare_change_id(self, cq_spec):
        assert cq_spec("7706944") == "chromium-review.googlesource.com/c/7706944/1"

    def test_chromium_url_keeps_no_project(self, cq_spec):
        url = "https://chromium-review.googlesource.com/c/chromium/src/+/8174803/1"
        assert cq_spec(url, patchset=3) == (
            "chromium-review.googlesource.com/c/8174803/3"
        )

    def test_v8_url(self, cq_spec):
        url = "https://chromium-review.googlesource.com/c/v8/v8/+/7650974"
        assert cq_spec(url, patchset=2) == (
            "chromium-review.googlesource.com/c/7650974/2"
        )
