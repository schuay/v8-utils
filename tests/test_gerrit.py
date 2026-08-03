import asyncio
import types

import httpx
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


# Gerrit's own per-label verdict. The numeric range is per-project (v8/v8's
# Code-Review is -1..+1), so a caller cannot infer "stands approved" from the
# vote values alone -- these flags are the portable signal.
class TestLabelFlags:
    def test_approved_and_rejected_are_surfaced(self):
        labels = {
            "Code-Review": {"approved": {"email": "a@b.c"}, "all": []},
            "Verified": {"rejected": {"email": "d@e.f"}, "all": []},
        }
        assert g._extract_label_flags(labels) == {
            "Code-Review": {"approved": True, "rejected": False},
            "Verified": {"approved": False, "rejected": True},
        }

    def test_label_with_no_verdict_is_omitted(self):
        # A label carrying only non-decisive votes says nothing either way.
        labels = {"Code-Review": {"all": [{"email": "a@b.c", "value": 0}]}}
        assert g._extract_label_flags(labels) == {}

    def test_min_and_max_together_report_only_approved(self):
        # Real case: v8/v8 8136620 carries -1 and +1; gerrit sets `approved` and
        # omits `rejected`, while submittable stays false. A caller must decide a
        # veto from the vote VALUES, never from the absence of this flag.
        labels = {
            "Code-Review": {
                "approved": {"email": "yes@b.c"},
                "all": [
                    {"email": "no@b.c", "value": -1},
                    {"email": "yes@b.c", "value": 1},
                ],
            }
        }
        flags = g._extract_label_flags(labels)
        assert flags["Code-Review"] == {"approved": True, "rejected": False}
        # ...and the values still carry the objection.
        assert g._extract_label_scores(labels)["Code-Review"] == [
            ("no@b.c", -1),
            ("yes@b.c", 1),
        ]

    def test_compact_change_carries_flags_alongside_scores(self):
        change = {
            "_number": 1,
            "labels": {
                "Code-Review": {
                    "approved": {"email": "a@b.c"},
                    "all": [{"email": "a@b.c", "value": 1}],
                }
            },
        }
        out = g._compact_change(change)
        assert out["labels"] == {"Code-Review": [("a@b.c", 1)]}
        assert out["label_flags"] == {
            "Code-Review": {"approved": True, "rejected": False}
        }


# ── _get: authenticated when possible, anonymous when not ────────────────────


def _resp(status, body='{"a": 1}'):
    return httpx.Response(status, text=body, request=httpx.Request("GET", "http://x"))


def test_get_prefers_the_authenticated_endpoint_when_a_token_exists(monkeypatch):
    """Gerrit's ANONYMOUS quota is small and shared per IP -- it covers every
    tool on the box plus any human browsing at the same time. A public CL
    answers 200 anonymously, so trying anonymous first meant a poll never
    upgraded and drew from that shared bucket forever. Measured against the real
    host: 25 rapid anonymous reads of a public CL returned 429 eleven times,
    while 25 authenticated ones returned 200 every time.
    """
    seen = []
    monkeypatch.setattr(g, "_gerrit_token", lambda: "tok")
    monkeypatch.setattr(
        httpx, "get", lambda url, **kw: seen.append((url, kw)) or _resp(200)
    )

    g._get("https://h", "/changes/1/comments")

    url, kw = seen[0]
    assert url == "https://h/a/changes/1/comments", "did not use the /a endpoint"
    assert kw["headers"]["Authorization"] == "Bearer tok"
    assert len(seen) == 1, "an authenticated 200 must not be retried"


def test_get_falls_back_to_anonymous_without_a_token(monkeypatch):
    # A box with no luci credentials still works against public CLs -- which is
    # why this is not simply auth_required=True everywhere.
    seen = []
    monkeypatch.setattr(g, "_gerrit_token", lambda: None)
    monkeypatch.setattr(
        httpx, "get", lambda url, **kw: seen.append((url, kw)) or _resp(200)
    )

    g._get("https://h", "/changes/1/comments")

    assert seen[0][0] == "https://h/changes/1/comments"
    assert "headers" not in seen[0][1]


def test_an_expired_token_degrades_to_anonymous_rather_than_raising(monkeypatch):
    # An expired or wrong-account token must not be WORSE than no token: on a
    # public endpoint the read still has to work, rather than raising the
    # "credentials missing or expired" error at a caller that never needed them.
    seen = []

    def fake_get(url, **kw):
        seen.append(url)
        return _resp(401) if "/a/" in url else _resp(200)

    monkeypatch.setattr(g, "_gerrit_token", lambda: "stale")
    monkeypatch.setattr(httpx, "get", fake_get)

    assert g._get("https://h", "/changes/1/comments") == {"a": 1}
    assert seen == ["https://h/a/changes/1/comments", "https://h/changes/1/comments"]


def test_auth_required_still_raises_rather_than_going_anonymous(monkeypatch):
    # /drafts and friends: anonymous is not merely slower there, it is wrong.
    monkeypatch.setattr(g, "_gerrit_token", lambda: None)
    with pytest.raises(ValueError, match="authentication required"):
        g._get("https://h", "/changes/1/drafts", auth_required=True)


def test_an_auth_required_401_does_not_silently_fall_back(monkeypatch):
    seen = []

    def fake_get(url, **kw):
        seen.append(url)
        return _resp(403)

    monkeypatch.setattr(g, "_gerrit_token", lambda: "stale")
    monkeypatch.setattr(httpx, "get", fake_get)
    with pytest.raises(ValueError, match="permission denied"):
        g._get("https://h", "/changes/1/drafts", auth_required=True)
    assert len(seen) == 1
