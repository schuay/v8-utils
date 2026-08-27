"""Unit tests for luci_auth — token minting and credential-helper suppression."""

import subprocess

import pytest

from v8_utils import luci_auth


@pytest.fixture
def captured_env(monkeypatch):
    """Record the env mint_token hands to luci-auth, without running it."""
    seen = {}

    def fake_check_output(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["env"] = kwargs.get("env")
        return "ya29.token\n"

    monkeypatch.setattr(luci_auth.subprocess, "check_output", fake_check_output)
    return seen


def test_credential_helper_is_disabled(captured_env, monkeypatch):
    """An SSO helper in the ambient env must not reach luci-auth.

    Corp workstations set this to a helper whose OAuth client id chromeperf
    does not allowlist, and the resulting 403 is indistinguishable from a
    permission problem.
    """
    monkeypatch.setenv("LUCI_AUTH_CREDENTIAL_HELPER", "/usr/bin/sso-cred-helper")

    luci_auth.mint_token()

    assert captured_env["env"]["LUCI_AUTH_CREDENTIAL_HELPER"] == ""


def test_rest_of_env_is_preserved(captured_env, monkeypatch):
    """Suppression must not cost luci-auth the rest of its environment."""
    monkeypatch.setenv("HOME", "/home/someone")

    luci_auth.mint_token()

    assert captured_env["env"]["HOME"] == "/home/someone"


def test_token_is_stripped(captured_env):
    assert luci_auth.mint_token() == "ya29.token"
    assert captured_env["cmd"] == ["luci-auth", "token"]


@pytest.mark.parametrize(
    "exc",
    [
        subprocess.CalledProcessError(1, "luci-auth", output="no creds"),
        FileNotFoundError(),
    ],
)
def test_failures_propagate(monkeypatch, exc):
    """Callers word these for their own surface, so mint_token must not eat them."""

    def fail(cmd, **kwargs):
        raise exc

    monkeypatch.setattr(luci_auth.subprocess, "check_output", fail)
    with pytest.raises(type(exc)):
        luci_auth.mint_token()
