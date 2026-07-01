import v8_utils.gerrit as g


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
