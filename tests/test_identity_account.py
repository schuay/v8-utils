"""The verified Gerrit account: what verify_gerrit remembers, who trusts it,
and how `self` queries and uploads use it."""

from __future__ import annotations

import subprocess
import types

import httpx
import pytest

import v8_utils.gerrit as g
from v8_utils import config, identity, trust
from v8_utils.api import identity as api_identity
from v8_utils.identity import GerritAccount, Impersonate, LuciAuth

BOT = "bot@proj.iam.gserviceaccount.com"
ACCOUNT = GerritAccount(email=BOT, name="Bot", account_id=42)


@pytest.fixture
def gerrit_self(monkeypatch):
    """verify_gerrit against a fake /accounts/self; returns the dict to edit."""
    me = {"_account_id": 42, "email": BOT, "name": "Bot"}
    monkeypatch.setattr(api_identity, "token", lambda use: "tok")
    monkeypatch.setattr(g, "account_self", lambda tok: me)
    return me


# ── verify_gerrit ─────────────────────────────────────────────────────────────


def test_verify_returns_and_remembers_the_account(gerrit_self):
    identity.configure({"bot": Impersonate(BOT, gerrit_account=42)}, {"gerrit": "bot"})
    assert identity.gerrit_account() is None
    got = api_identity.verify_gerrit()
    assert got == ACCOUNT
    assert str(got) == f"{BOT} (account 42)"
    assert identity.gerrit_account() == ACCOUNT
    assert identity.gerrit_email() == BOT


def test_verify_refuses_a_mismatched_account(gerrit_self):
    gerrit_self["_account_id"] = 7
    identity.configure({"bot": Impersonate(BOT, gerrit_account=42)}, {"gerrit": "bot"})
    with pytest.raises(identity.IdentityError, match="mismatch"):
        api_identity.verify_gerrit()
    assert identity.gerrit_account() is None


def test_verify_refuses_an_unusable_self(gerrit_self):
    del gerrit_self["email"]
    with pytest.raises(identity.IdentityError, match="no usable account"):
        api_identity.verify_gerrit()


def test_reconfigure_forgets_the_account(gerrit_self):
    api_identity.verify_gerrit()
    identity.configure({"op": LuciAuth()}, {"gerrit": "op"})
    assert identity.gerrit_account() is None


# ── gerrit_email without a round trip ────────────────────────────────────────


def test_email_is_unknown_for_an_unverified_host_login():
    assert identity.gerrit_email() is None


def test_email_of_an_impersonated_principal_is_the_service_account():
    identity.configure({"bot": Impersonate(BOT)}, {"gerrit": "bot"})
    assert identity.gerrit_email() == BOT


# ── trust ─────────────────────────────────────────────────────────────────────


def test_own_account_is_trusted_outside_the_domains():
    trust.configure(["chromium.org"])
    assert not trust.is_trusted(BOT)
    identity.remember_gerrit_account(ACCOUNT)
    assert trust.is_trusted(BOT)
    assert trust.is_trusted(f" {BOT.upper()} ")
    assert not trust.is_trusted("other@proj.iam.gserviceaccount.com")


def test_own_cl_is_readable():
    trust.configure(["chromium.org"])
    account = {"_account_id": 42, "email": BOT, "name": "Bot"}
    change = {
        "_number": 7,
        "status": "NEW",
        "owner": account,
        "current_revision": "a" * 40,
        "revisions": {"a" * 40: {"_number": 1, "uploader": account}},
    }
    assert trust.untrusted_change_reason(change)
    assert trust.untrusted_patchset_reason(change, 1)
    identity.remember_gerrit_account(ACCOUNT)
    assert trust.untrusted_change_reason(change) is None
    assert trust.untrusted_patchset_reason(change, 1) is None


# ── self queries ──────────────────────────────────────────────────────────────


def test_self_resolves_to_the_acting_account(monkeypatch):
    monkeypatch.setattr(
        config, "load", lambda: types.SimpleNamespace(user="me@chromium.org")
    )
    assert g._resolve_self("owner:self") == "owner:me@chromium.org"
    identity.remember_gerrit_account(ACCOUNT)
    assert g._resolve_self("owner:self is:open") == f"owner:{BOT} is:open"


# ── upload ────────────────────────────────────────────────────────────────────

URL = "https://chromium-review.googlesource.com"
SHA = "c" * 40


@pytest.fixture
def push(monkeypatch):
    """Capture the git push command; `out` is what git prints."""
    seen: dict = {"out": "", "code": 0}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["cwd"] = kw.get("cwd")
        return subprocess.CompletedProcess(cmd, seen["code"], "", seen["out"])

    monkeypatch.setattr(g.subprocess, "run", fake_run)
    monkeypatch.setattr(g, "_require_auth", lambda: "tok-gerrit")
    return seen


def test_push_targets_refs_for_with_a_bearer_header(push):
    push["out"] = f"remote:   {URL}/c/v8/v8/+/123 Fix the thing [NEW]\n"
    r = g.push_for_review(
        "/repo", SHA, project="v8/v8", branch="main", options=["hashtag=x", "wip"]
    )
    assert r == g.PushResult(
        change_number=123, url=f"{URL}/c/v8/v8/+/123", created=True
    )
    cmd = push["cmd"]
    assert push["cwd"] == "/repo"
    assert cmd[-2:] == [
        "https://chromium.googlesource.com/v8/v8",
        f"{SHA}:refs/for/main%hashtag=x,wip",
    ]
    assert (
        "http.https://chromium.googlesource.com/v8/v8/.extraHeader="
        "Authorization: Bearer tok-gerrit"
    ) in cmd
    assert "http.cookiefile=" in cmd
    assert "credential.helper=" in cmd


def test_push_of_an_identical_commit_resolves_the_existing_change(push, monkeypatch):
    push["out"] = "! [remote rejected] HEAD -> refs/for/main (no new changes)\n"
    push["code"] = 1
    monkeypatch.setattr(
        g, "_get", lambda base, path, **kw: [{"_number": 456, "project": "v8/v8"}]
    )
    r = g.push_for_review("/repo", SHA, project="v8/v8", branch="main")
    assert r == g.PushResult(
        change_number=456, url=f"{URL}/c/v8/v8/+/456", created=False
    )


def test_push_failure_reports_stderr_without_the_token(push):
    push["out"] = "fatal: Authentication failed for tok-gerrit\n"
    push["code"] = 128
    with pytest.raises(RuntimeError, match="exit 128") as e:
        g.push_for_review("/repo", SHA, project="v8/v8", branch="main")
    assert "tok-gerrit" not in str(e.value)
    assert "<token>" in str(e.value)


def test_change_for_commit_requires_one_match(monkeypatch):
    monkeypatch.setattr(g, "_get", lambda base, path, **kw: [])
    with pytest.raises(RuntimeError, match="matches 0"):
        g.change_for_commit(SHA, project="v8/v8")


def test_change_id_for_reads_the_footer_value(monkeypatch):
    monkeypatch.setattr(
        g, "_get", lambda base, path, **kw: {"change_id": "I" + "a" * 40}
    )
    assert g.change_id_for(f"{URL}/c/v8/v8/+/123") == "I" + "a" * 40
    monkeypatch.setattr(g, "_get", lambda base, path, **kw: {})
    with pytest.raises(RuntimeError, match="no Change-Id"):
        g.change_id_for(f"{URL}/c/v8/v8/+/123")


def test_patchset_for_commit_retries_until_gerrit_shows_it(monkeypatch):
    calls = []

    def fake_get(base, path, **kw):
        calls.append(path)
        if len(calls) < 3:
            resp = httpx.Response(404, request=httpx.Request("GET", base + path))
            raise httpx.HTTPStatusError("404", request=resp.request, response=resp)
        return {"_number": 4}

    monkeypatch.setattr(g, "_get", fake_get)
    monkeypatch.setattr(g.time, "sleep", lambda s: None)
    assert g.patchset_for_commit(f"{URL}/c/v8/v8/+/123", SHA, attempts=5) == 4
    assert calls == [f"/changes/v8%2Fv8~123/revisions/{SHA}"] * 3


def test_patchset_for_commit_gives_up(monkeypatch):
    def fake_get(base, path, **kw):
        resp = httpx.Response(404, request=httpx.Request("GET", base + path))
        raise httpx.HTTPStatusError("404", request=resp.request, response=resp)

    monkeypatch.setattr(g, "_get", fake_get)
    monkeypatch.setattr(g.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="not visible after 2 attempts"):
        g.patchset_for_commit(f"{URL}/c/v8/v8/+/123", SHA, attempts=2)


def test_set_review_votes_and_keeps_drafts(monkeypatch):
    seen = {}

    def fake_post(base, path, body):
        seen["path"] = path
        seen["body"] = body
        return {"labels": {"Commit-Queue": 1}}

    monkeypatch.setattr(g, "_post_json", fake_post)
    r = g.set_review(
        f"{URL}/c/v8/v8/+/123",
        labels={"Commit-Queue": 1},
        message="dry run",
        patchset="3",
    )
    assert r.labels == {"Commit-Queue": 1}
    assert seen["path"] == "/changes/v8%2Fv8~123/revisions/3/review"
    assert seen["body"] == {
        "drafts": "KEEP",
        "labels": {"Commit-Queue": 1},
        "message": "dry run",
    }
