"""v8_utils.identity: use -> principal mapping, token minting, failure wording."""

from __future__ import annotations

import subprocess

import pytest

from v8_utils import identity
from v8_utils.identity import Impersonate, LuciAuth

BOT = "bot@proj.iam.gserviceaccount.com"


# ── configuration ────────────────────────────────────────────────────────────


def test_defaults_to_the_host_login_for_every_use():
    for use in identity.USES:
        assert isinstance(identity.principal(use), LuciAuth)


def test_configure_maps_uses_to_declared_principals():
    identity.configure(
        {"bot": Impersonate(BOT, gerrit_account=42), "operator": LuciAuth()},
        {"gerrit": "bot", "pinpoint": "operator"},
    )
    assert identity.principal("gerrit") == Impersonate(BOT, gerrit_account=42)
    assert identity.principal("pinpoint") == LuciAuth()
    # An unmapped use keeps the default rather than failing.
    assert identity.principal("cas") == LuciAuth()


def test_configure_refuses_unknown_use_and_undeclared_principal():
    with pytest.raises(ValueError, match="unknown identity use"):
        identity.configure({"bot": Impersonate(BOT)}, {"rbe": "bot"})
    with pytest.raises(ValueError, match="undeclared principal"):
        identity.configure({"bot": Impersonate(BOT)}, {"gerrit": "operator"})


def test_impersonate_requires_an_email():
    with pytest.raises(ValueError, match="not a service account email"):
        Impersonate("bot")


def test_describe_names_the_account():
    identity.configure({"bot": Impersonate(BOT)}, {"gerrit": "bot"})
    assert BOT in identity.describe("gerrit")
    assert "luci-auth" in identity.describe("pinpoint")


# ── host login ───────────────────────────────────────────────────────────────


def test_gerrit_on_the_host_login_uses_git_credential_luci(monkeypatch):
    """Gerrit tokens come from git-credential-luci, not `luci-auth token`: the
    helper's own OAuth client is the one Gerrit expects."""
    seen = {}

    def fake_check_output(cmd, **kw):
        seen["cmd"] = cmd
        return "username=git-luci\npassword=tok-gerrit\n"

    monkeypatch.setattr(identity.subprocess, "check_output", fake_check_output)
    assert identity.token("gerrit") == "tok-gerrit"
    assert seen["cmd"][0] == "git-credential-luci"


def test_other_uses_on_the_host_login_use_luci_auth(monkeypatch):
    monkeypatch.setattr(identity.luci_auth, "mint_token", lambda: "tok-luci")
    assert identity.token("pinpoint") == "tok-luci"
    assert identity.token("cas") == "tok-luci"


def test_host_login_failures_name_the_fix(monkeypatch):
    def missing(*a, **kw):
        raise FileNotFoundError("git-credential-luci")

    monkeypatch.setattr(identity.subprocess, "check_output", missing)
    with pytest.raises(identity.IdentityError, match="not on PATH"):
        identity.token("gerrit")

    def not_logged_in():
        raise subprocess.CalledProcessError(1, ["luci-auth"], output="Not logged in.")

    monkeypatch.setattr(identity.luci_auth, "mint_token", not_logged_in)
    with pytest.raises(identity.IdentityError, match="luci-auth login") as e:
        identity.token("pinpoint")
    assert "Not logged in." in str(e.value)


def test_try_token_returns_none_and_keeps_the_reason(monkeypatch):
    def missing(*a, **kw):
        raise FileNotFoundError("git-credential-luci")

    monkeypatch.setattr(identity.subprocess, "check_output", missing)
    assert identity.try_token("gerrit") is None
    assert "not on PATH" in identity.unavailable_reason("gerrit")
    # A use that was never asked says so instead of inventing a cause.
    assert "no token has been requested" in identity.unavailable_reason("cas")


# ── impersonation ────────────────────────────────────────────────────────────


class _FakeCreds:
    """Stands in for google.auth impersonated credentials: a token that goes
    invalid once and is refreshed in place."""

    instances: list[_FakeCreds] = []

    def __init__(
        self, *, source_credentials, target_principal, target_scopes, lifetime
    ):
        self.target_principal = target_principal
        self.target_scopes = target_scopes
        self.valid = False
        self.token = None
        self.refreshes = 0
        _FakeCreds.instances.append(self)

    def refresh(self, request):
        self.refreshes += 1
        self.valid = True
        self.token = f"ya29.{self.target_principal}.{self.refreshes}"


@pytest.fixture
def fake_google_auth(monkeypatch):
    from google.auth import impersonated_credentials

    _FakeCreds.instances = []
    monkeypatch.setattr(impersonated_credentials, "Credentials", _FakeCreds)
    monkeypatch.setattr(identity.gauth, "credentials", lambda scopes=None: object())
    return _FakeCreds


def test_impersonation_mints_for_the_use_scopes_and_caches(fake_google_auth):
    identity.configure({"bot": Impersonate(BOT)}, {"gerrit": "bot", "pinpoint": "bot"})

    first = identity.token("gerrit")
    second = identity.token("gerrit")
    assert first == second == f"ya29.{BOT}.1"
    assert len(fake_google_auth.instances) == 1
    assert fake_google_auth.instances[0].target_scopes == [identity.GERRIT_SCOPE]

    # A different use carries different scopes, so it is a different credential.
    identity.token("pinpoint")
    assert len(fake_google_auth.instances) == 2
    assert fake_google_auth.instances[1].target_scopes == [identity.EMAIL_SCOPE]


def test_an_expired_impersonated_credential_is_refreshed_in_place(fake_google_auth):
    identity.configure({"bot": Impersonate(BOT)}, {"gerrit": "bot"})
    identity.token("gerrit")
    creds = fake_google_auth.instances[0]
    creds.valid = False
    assert identity.token("gerrit") == f"ya29.{BOT}.2"
    assert len(fake_google_auth.instances) == 1


def test_a_denied_impersonation_names_the_iam_fix(fake_google_auth, monkeypatch):
    def denied(self, request):
        raise RuntimeError("403 Permission 'iam.serviceAccounts.getAccessToken' denied")

    monkeypatch.setattr(fake_google_auth, "refresh", denied)
    identity.configure({"bot": Impersonate(BOT)}, {"gerrit": "bot"})
    with pytest.raises(identity.IdentityError, match="serviceAccountTokenCreator"):
        identity.token("gerrit")
    assert "serviceAccountTokenCreator" in identity.unavailable_reason("gerrit")


def test_a_missing_adc_file_is_worded_for_the_bot(monkeypatch):
    def no_adc(scopes=None):
        raise FileNotFoundError("no application default credentials at ~/x; run: login")

    monkeypatch.setattr(identity.gauth, "credentials", no_adc)
    identity.configure({"bot": Impersonate(BOT)}, {"gerrit": "bot"})
    with pytest.raises(identity.IdentityError, match=f"cannot impersonate {BOT}"):
        identity.token("gerrit")


def test_reconfigure_drops_cached_credentials(fake_google_auth):
    identity.configure({"bot": Impersonate(BOT)}, {"gerrit": "bot"})
    identity.token("gerrit")
    identity.configure({"bot": Impersonate(BOT)}, {"gerrit": "bot"})
    identity.token("gerrit")
    assert len(fake_google_auth.instances) == 2
