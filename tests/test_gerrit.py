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


def test_a_missing_helper_and_a_missing_login_read_differently(monkeypatch):
    """The two ways to have no token need opposite fixes, and _gerrit_token
    returns None for both.

    Naming only the login sent an operator whose systemd unit simply lacked
    depot_tools on PATH to re-run `login` -- which succeeds and changes nothing.
    """
    monkeypatch.setattr(g, "_gerrit_token", lambda: None)

    monkeypatch.setattr(g.shutil, "which", lambda _: None)
    with pytest.raises(ValueError, match="not on PATH") as missing:
        g._require_auth()
    # Points at the cause, not at authenticating again.
    assert "login" not in str(missing.value)

    monkeypatch.setattr(g.shutil, "which", lambda _: "/usr/bin/git-credential-luci")
    with pytest.raises(ValueError, match="git-credential-luci login") as unauthed:
        g._require_auth()
    assert "not on PATH" not in str(unauthed.value)


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


# ── create_drafts / publish_drafts ───────────────────────────────────────────


@pytest.fixture
def drafts(monkeypatch):
    """Capture create_drafts' PUTs, with a fixed set of published parents.

    The published payload is keyed by file with `path` absent from the
    CommentInfo, exactly as gerrit returns it.
    """
    sent = []
    reads = []
    published = {
        "src/wasm/wasm-objects.cc": [
            {
                "id": "c1",
                "patch_set": 5,
                "line": 259,
                "side": "REVISION",
                "range": {
                    "start_line": 259,
                    "start_character": 7,
                    "end_line": 259,
                    "end_character": 20,
                },
                "message": "nit: capitalize",
            },
            {"id": "c-file", "patch_set": 5, "message": "file-level thought"},
        ],
        "/PATCHSET_LEVEL": [
            {"id": "c-top", "patch_set": 5, "message": "overall"},
        ],
        "/COMMIT_MSG": [
            {"id": "c-msg", "patch_set": 5, "line": 18, "message": "drop this line"},
        ],
    }

    def fake_get(base, path, **kw):
        reads.append(path)
        return published

    monkeypatch.setattr(g, "_get", fake_get)
    monkeypatch.setattr(
        g,
        "_put_json",
        lambda base, path, body: sent.append((path, body)) or {"id": "d1"},
    )
    return types.SimpleNamespace(sent=sent, reads=reads, published=published)


CL = "https://chromium-review.googlesource.com/8174846"


def test_a_reply_draft_carries_in_reply_to_and_the_ai_marker(drafts):
    out = g.create_drafts(
        CL, [{"message": "Done.", "in_reply_to": "c1", "is_ai": True}]
    )

    assert out[0]["ok"] is True
    _path, body = drafts.sent[0]
    assert body["in_reply_to"] == "c1"
    # A reviewer is entitled to know a reply was not typed by a human.
    assert body["is_ai"] is True


def test_a_bare_reply_lands_on_its_parents_location(drafts):
    """in_reply_to alone does not place a comment.

    Gerrit stores the location exactly as sent (CreateDraftComment only tests
    that the parent exists), and its thread builder sorts by path before it
    walks in_reply_to -- with /PATCHSET_LEVEL ahead of every real file. A reply
    left at the default path is visited before the parent it names, misses the
    lookup, and renders as its own detached thread.
    """
    g.create_drafts(CL, [{"message": "Done.", "in_reply_to": "c1"}])

    path, body = drafts.sent[0]
    assert body["path"] == "src/wasm/wasm-objects.cc"
    assert body["line"] == 259
    assert body["range"] == drafts.published["src/wasm/wasm-objects.cc"][0]["range"]
    assert body["side"] == "REVISION"
    # ...and on the parent's patchset, so it shows in that diff view too.
    assert path.endswith("/revisions/5/drafts")


def test_a_reply_to_a_top_level_comment_stays_top_level(drafts):
    # The inherited path is the magic one, so side/line must stay off the body:
    # gerrit rejects them on patchset-level drafts.
    g.create_drafts(CL, [{"message": "Done.", "in_reply_to": "c-top"}])

    _path, body = drafts.sent[0]
    assert body["path"] == "/PATCHSET_LEVEL"
    assert "side" not in body and "line" not in body and "range" not in body


def test_a_reply_to_a_commit_message_comment_lands_on_commit_msg(drafts):
    # The bug's other face: these were filed at /PATCHSET_LEVEL too.
    g.create_drafts(CL, [{"message": "Done.", "in_reply_to": "c-msg"}])

    _path, body = drafts.sent[0]
    assert body["path"] == "/COMMIT_MSG"
    assert body["line"] == 18


def test_a_reply_to_a_file_level_comment_inherits_no_line(drafts):
    g.create_drafts(CL, [{"message": "Done.", "in_reply_to": "c-file"}])

    _path, body = drafts.sent[0]
    assert body["path"] == "src/wasm/wasm-objects.cc"
    assert "line" not in body and "range" not in body


def test_a_new_comment_still_defaults_to_top_level(drafts):
    # No in_reply_to: nothing to inherit, and no reason to read the parents.
    g.create_drafts(CL, [{"message": "overall thought"}])

    path, body = drafts.sent[0]
    assert body["path"] == "/PATCHSET_LEVEL"
    assert "side" not in body and "line" not in body
    assert path.endswith("/revisions/current/drafts")
    assert drafts.reads == []


@pytest.mark.parametrize(
    "pin",
    [
        {"path": "src/other.cc", "line": 12},
        {"path": "src/other.cc"},
        {"line": 12},
        {"range": {"start_line": 12, "end_line": 12}},
    ],
)
def test_a_reply_that_pins_a_location_keeps_it(drafts, pin):
    """Location is inherited as a unit, never merged field by field.

    Half the caller's coordinates and half the parent's would put the comment
    somewhere neither asked for.
    """
    g.create_drafts(CL, [{"message": "see here", "in_reply_to": "c1", **pin}])

    _path, body = drafts.sent[0]
    if "path" in pin:
        assert body["path"] == pin["path"]
        assert body.get("line") == pin.get("line")
        assert body.get("range") == pin.get("range")
    else:
        # A line with no path was meaningless before this and still is; what
        # matters is that it does not drag half the parent's location along.
        assert body["path"] == "/PATCHSET_LEVEL"
        assert "line" not in body and "range" not in body and "side" not in body


def test_the_parents_are_read_once_for_a_batch_of_replies(drafts):
    g.create_drafts(
        CL,
        [
            {"message": "a", "in_reply_to": "c1"},
            {"message": "b", "in_reply_to": "c-top"},
            {"message": "c", "in_reply_to": "c-msg"},
        ],
    )
    assert len(drafts.sent) == 3
    assert len(drafts.reads) == 1


def test_an_unknown_parent_is_left_for_gerrit_to_reject(drafts, monkeypatch):
    """Gerrit's own error names the uuid; a local guess would not be better.

    The comment may simply be newer than our read, or a draft rather than
    published.
    """
    monkeypatch.setattr(
        g,
        "_put_json",
        lambda base, path, body: (_ for _ in ()).throw(
            RuntimeError("HTTP 400: Invalid inReplyTo, comment nope not found")
        ),
    )
    (out,) = g.create_drafts(CL, [{"message": "Done.", "in_reply_to": "nope"}])
    assert out["ok"] is False
    assert "Invalid inReplyTo" in out["error"]


def test_an_unreadable_parent_fails_the_reply_instead_of_relocating_it(
    drafts, monkeypatch
):
    # Falling back to /PATCHSET_LEVEL is the bug this all guards against, so a
    # failed read must not silently take that path.
    def boom(base, path, **kw):
        raise RuntimeError("HTTP 429: too many requests")

    monkeypatch.setattr(g, "_get", boom)
    (out,) = g.create_drafts(CL, [{"message": "Done.", "in_reply_to": "c1"}])

    assert out["ok"] is False
    assert "429" in out["error"]
    assert drafts.sent == []


def test_an_explicit_patchset_does_not_move_an_inherited_reply(drafts):
    # The thread lives on the parent's patchset; a stale default must not drag
    # the reply off it.
    g.create_drafts(CL, [{"message": "Done.", "in_reply_to": "c1"}], patchset=7)
    assert drafts.sent[0][0].endswith("/revisions/5/drafts")


def test_an_explicit_patchset_still_governs_a_pinned_comment(drafts):
    g.create_drafts(
        CL, [{"message": "here", "path": "src/other.cc", "line": 3}], patchset=7
    )
    assert drafts.sent[0][0].endswith("/revisions/7/drafts")


def test_one_failing_draft_does_not_sink_the_others(monkeypatch):
    # Per-comment results are the point: an all-or-nothing post would make the
    # caller retry replies that already landed, duplicating them.
    def fake_put(base, path, body):
        if body.get("in_reply_to") == "bad":
            raise RuntimeError("HTTP 400: Invalid inReplyTo")
        return {"id": "ok"}

    monkeypatch.setattr(g, "_put_json", fake_put)
    out = g.create_drafts(
        "https://chromium-review.googlesource.com/8174846",
        [
            {"message": "a", "in_reply_to": "good"},
            {"message": "b", "in_reply_to": "bad"},
            {"message": "c", "in_reply_to": "good"},
        ],
    )
    assert [r["ok"] for r in out] == [True, False, True]
    assert "Invalid inReplyTo" in out[1]["error"]
    assert out[1]["input"]["message"] == "b"  # enough to retry just this one


def test_publishing_sends_every_draft_and_votes_on_nothing(monkeypatch):
    """Gerrit has no publish endpoint: it is a review post with drafts=PUBLISH*.

    PUBLISH_ALL_REVISIONS rather than PUBLISH because a caller that drafted
    replies against the patchset a comment was left on, then uploaded a newer
    one, would otherwise publish nothing.
    """
    sent = []
    monkeypatch.setattr(
        g, "_post_json", lambda base, path, body: sent.append((path, body)) or {}
    )

    g.publish_drafts(
        "https://chromium-review.googlesource.com/8174846", message="Addressed."
    )

    path, body = sent[0]
    assert path.endswith("/revisions/current/review")
    assert body["drafts"] == "PUBLISH_ALL_REVISIONS"
    assert body["message"] == "Addressed."
    # Publishing a reply must never vote on the CL.
    assert "labels" not in body


# ── post_review_comments ─────────────────────────────────────────────────────


@pytest.fixture
def review_posts(monkeypatch):
    """Capture post_review_comments' single POST as (path, body)."""
    sent = []
    monkeypatch.setattr(
        g, "_post_json", lambda base, path, body: sent.append((path, body)) or {"ok": 1}
    )
    return sent


def test_review_post_batches_every_comment_into_one_request(review_posts):
    g.post_review_comments(
        "https://chromium-review.googlesource.com/c/v8/v8/+/8174846",
        [
            {"path": "src/a.cc", "line": 10, "message": "first"},
            {"path": "src/a.cc", "line": 20, "message": "second"},
            {"path": "src/b.cc", "line": 5, "message": "third", "unresolved": False},
        ],
        message="perf review",
    )

    # One request, whatever the comment count -- the point of this primitive.
    assert len(review_posts) == 1
    path, body = review_posts[0]
    assert path == "/changes/v8%2Fv8~8174846/revisions/current/review"
    # Keyed by file, several comments per file preserved in order.
    assert [c["message"] for c in body["comments"]["src/a.cc"]] == ["first", "second"]
    assert body["comments"]["src/b.cc"][0]["line"] == 5
    assert body["message"] == "perf review"
    # unresolved defaults True and is honoured per comment.
    assert body["comments"]["src/a.cc"][0]["unresolved"] is True
    assert body["comments"]["src/b.cc"][0]["unresolved"] is False
    # Posting review comments must never vote on the CL.
    assert "labels" not in body


def test_review_post_keeps_existing_drafts(review_posts):
    """DraftHandling defaults to DELETE, so omitting `drafts` would discard the
    caller's unpublished drafts on the revision -- someone else's data, lost
    silently. depot_tools sends KEEP on every SetReview for this reason."""
    g.post_review_comments(
        "https://chromium-review.googlesource.com/8174846",
        [{"path": "src/a.cc", "line": 1, "message": "x"}],
    )
    assert review_posts[0][1]["drafts"] == "KEEP"


def test_review_post_marks_machine_authored_comments(review_posts):
    g.post_review_comments(
        "https://chromium-review.googlesource.com/8174846",
        [{"path": "src/a.cc", "line": 1, "message": "x", "is_ai": True}],
    )
    assert review_posts[0][1]["comments"]["src/a.cc"][0]["is_ai"] is True


def test_review_post_files_a_pathless_comment_at_change_level(review_posts):
    """No path means a change-level comment, and every /PATCHSET_LEVEL/ spelling
    normalizes to the canonical one -- a trailing slash makes gerrit treat it as
    a literal file nobody can see. Gerrit also rejects line/side/range there."""
    g.post_review_comments(
        "https://chromium-review.googlesource.com/8174846",
        [
            {"message": "overall: looks good"},
            {"path": "/PATCHSET_LEVEL/", "line": 3, "message": "also top-level"},
        ],
    )
    filed = review_posts[0][1]["comments"]
    assert list(filed) == ["/PATCHSET_LEVEL"]
    assert [c["message"] for c in filed["/PATCHSET_LEVEL"]] == [
        "overall: looks good",
        "also top-level",
    ]
    assert not any(k in filed["/PATCHSET_LEVEL"][1] for k in ("line", "side", "range"))


def test_review_post_pins_the_patchset_it_was_given(review_posts):
    """A review written against patchset 3 must not land on a 4 that was uploaded
    mid-review -- those comments would be about code nobody read."""
    g.post_review_comments(
        "https://chromium-review.googlesource.com/8174846",
        [{"path": "src/a.cc", "line": 1, "message": "x"}],
        patchset=3,
    )
    assert review_posts[0][0].endswith("/revisions/3/review")


def test_review_post_validates_before_sending_anything(review_posts):
    """The request is atomic, so a bad entry must be caught BEFORE the POST --
    otherwise gerrit rejects the whole batch and the good comments are lost with
    an error naming none of them."""
    with pytest.raises(ValueError, match="comment 1: missing field: message"):
        g.post_review_comments(
            "https://chromium-review.googlesource.com/8174846",
            [{"path": "a.cc", "line": 1, "message": "ok"}, {"path": "a.cc", "line": 2}],
        )
    assert review_posts == []  # nothing reached gerrit


def test_review_post_refuses_a_reply(review_posts):
    """in_reply_to cannot be honoured by a path-keyed review post: without the
    parent lookup create_drafts does, the reply would silently file as a detached
    top-level comment. Refuse loudly and name the alternative."""
    with pytest.raises(ValueError, match="in_reply_to is not supported"):
        g.post_review_comments(
            "https://chromium-review.googlesource.com/8174846",
            [{"message": "agreed", "in_reply_to": "c1"}],
        )
    assert review_posts == []


def test_review_post_refuses_an_empty_review(review_posts):
    # A bare {"drafts": "KEEP"} would be a no-op request that still reads as
    # success to the caller.
    with pytest.raises(ValueError, match="nothing to post"):
        g.post_review_comments("https://chromium-review.googlesource.com/8174846", [])
    assert review_posts == []


def test_review_post_may_carry_a_message_alone(review_posts):
    """A cover note with no inline comments is a legitimate review post; only the
    truly empty one is refused."""
    g.post_review_comments(
        "https://chromium-review.googlesource.com/8174846", [], message="lgtm"
    )
    body = review_posts[0][1]
    assert body["message"] == "lgtm" and "comments" not in body


def test_pinpoint_resolves_patches_through_the_authenticated_reader(monkeypatch):
    """pinpoint.py had its own bare httpx.get against Gerrit.

    Those never authenticated, so they drew on the ANONYMOUS quota -- small and
    shared per IP, hence 429s under no load of their own. Observed killing a
    `pp create-job` mid-run, after it had already queued jobs.
    """
    import v8_utils.pinpoint as pinpoint

    seen = []

    def fake_get(base, path, **kw):
        seen.append((base, path))
        return {"project": "v8/v8"}

    monkeypatch.setattr(g, "_get", fake_get)
    # A bare httpx.get here would bypass the fake and hit the network.
    monkeypatch.setattr(
        httpx, "get", lambda *a, **k: pytest.fail("unauthenticated Gerrit read")
    )

    url = pinpoint.resolve_patch("8176626")

    assert url.endswith("/c/v8/v8/+/8176626")
    assert seen == [("https://chromium-review.googlesource.com", "/changes/8176626")]


# ── comments(): thread building ──────────────────────────────────────────────


def _comments_payload():
    """One file with a root, an AI reply, and a human follow-up."""
    return {
        "src/a.cc": [
            {
                "id": "root",
                "author": {"email": "reviewer@chromium.org"},
                "message": "why?",
                "line": 7,
                "patch_set": 2,
                "unresolved": True,
                "updated": "2026-08-25 10:00:00.000000000",
            },
            {
                "id": "ours",
                "in_reply_to": "root",
                "author": {"email": "operator@chromium.org"},
                "message": "Done.",
                "is_ai": True,
                "unresolved": False,
                "updated": "2026-08-25 11:00:00.000000000",
            },
            {
                "id": "back",
                "in_reply_to": "ours",
                "author": {"email": "reviewer@chromium.org"},
                "message": "still not right",
                "unresolved": True,
                "updated": "2026-08-25 12:00:00.000000000",
            },
        ]
    }


def test_comments_passes_is_ai_through_on_replies(monkeypatch):
    # The flag is the only thing separating a caller's OWN replies from a
    # reviewer's: both are posted under the same human account.
    monkeypatch.setattr(g, "_get", lambda host, path: _comments_payload())
    (thread,) = g.comments("https://chromium-review.googlesource.com/123")
    assert [r["id"] for r in thread["replies"]] == ["ours", "back"]
    assert thread["replies"][0]["is_ai"] is True
    # Absent, not False: gerrit omits the field when unset, and "not marked"
    # is not the same claim as "a human wrote this".
    assert "is_ai" not in thread["replies"][1]
    assert "is_ai" not in thread


def test_comments_passes_is_ai_through_on_a_thread_root(monkeypatch):
    payload = {"src/a.cc": [{"id": "r", "message": "x", "is_ai": True}]}
    monkeypatch.setattr(g, "_get", lambda host, path: payload)
    (thread,) = g.comments("https://chromium-review.googlesource.com/123")
    assert thread["is_ai"] is True


def test_comments_reads_resolution_from_the_last_entry(monkeypatch):
    # A thread's standing is its LAST entry's, which is what gerrit's own UI
    # renders -- the root's flag says nothing about where the thread stands.
    monkeypatch.setattr(g, "_get", lambda host, path: _comments_payload())
    (thread,) = g.comments("https://chromium-review.googlesource.com/123")
    assert thread["unresolved"] is True


# ── resolve_patchset ──────────────────────────────────────────────────────────


def _change(current="sha3", patchsets=(1, 2, 3), project="v8/v8"):
    return {
        "project": project,
        "current_revision": current,
        "revisions": {f"sha{n}": {"_number": n} for n in patchsets},
    }


def _capture(monkeypatch, payload):
    seen = {}
    monkeypatch.setattr(
        g, "_get", lambda host, path: seen.update(host=host, path=path) or payload
    )
    return seen


def test_resolve_patchset_pins_the_current_revision(monkeypatch):
    seen = _capture(monkeypatch, _change())
    out = g.resolve_patchset(
        "https://chromium-review.googlesource.com/c/v8/v8/+/7650974"
    )
    assert out == {
        "ref": "refs/changes/74/7650974/3",
        "patchset": "3",
        "revision": "sha3",
        "project": "v8/v8",
        "host": "chromium-review.googlesource.com",
    }
    # One query, and the one that carries every patchset's SHA.
    assert "o=ALL_REVISIONS" in seen["path"]


def test_resolve_patchset_honours_a_patchset_in_the_url(monkeypatch):
    # The whole point of pinning: an approval binds to the patchset the human
    # read, not to whatever is current when the job finally runs.
    _capture(monkeypatch, _change())
    out = g.resolve_patchset("https://chromium-review.googlesource.com/7650974/1")
    assert out["patchset"] == "1"
    assert out["revision"] == "sha1"
    assert out["ref"] == "refs/changes/74/7650974/1"


def test_resolve_patchset_reads_the_project_off_the_response(monkeypatch):
    # A short-form URL carries no project, and a caller restricting citations to
    # its own repo has nothing else to check.
    _capture(monkeypatch, _change(project="chromium/src"))
    out = g.resolve_patchset("https://chromium-review.googlesource.com/7650974")
    assert out["project"] == "chromium/src"


def test_resolve_patchset_refuses_a_change_that_does_not_exist(monkeypatch):
    # fetch_ref(fetch=False) answers a URL naming a patchset without asking
    # gerrit anything, so a mistyped change resolves there and fails later,
    # wherever the ref is first used. This always asks.
    _capture(monkeypatch, {})
    with pytest.raises(ValueError, match="no such change"):
        g.resolve_patchset("https://chromium-review.googlesource.com/7650974/2")


def test_resolve_patchset_refuses_a_patchset_the_change_has_not_got(monkeypatch):
    _capture(monkeypatch, _change(patchsets=(1, 2)))
    with pytest.raises(ValueError, match="no patchset 9"):
        g.resolve_patchset("https://chromium-review.googlesource.com/7650974/9")


def test_resolve_patchset_refuses_a_change_with_no_current_revision(monkeypatch):
    _capture(monkeypatch, _change(current="gone"))
    with pytest.raises(ValueError, match="no current revision"):
        g.resolve_patchset("https://chromium-review.googlesource.com/7650974")
