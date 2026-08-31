"""Unit tests for luci_auth — token minting and credential-helper suppression."""

import subprocess

import pytest

from v8_utils import luci_auth


def _completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(
        ["luci-auth", "token"], returncode, stdout=stdout, stderr=stderr
    )


@pytest.fixture
def captured_env(monkeypatch):
    """Record the env mint_token hands to luci-auth, without running it."""
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["env"] = kwargs.get("env")
        return _completed(stdout="ya29.token\n")

    monkeypatch.setattr(luci_auth.subprocess, "run", fake_run)
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


def test_a_missing_binary_propagates(monkeypatch):
    """Callers word these for their own surface, so mint_token must not eat them."""

    def fail(cmd, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(luci_auth.subprocess, "run", fail)
    with pytest.raises(FileNotFoundError):
        luci_auth.mint_token()


def test_a_nonzero_exit_folds_both_streams_into_the_error(monkeypatch):
    """The documented contract: callers read CalledProcessError.output for
    luci-auth's own diagnostics. A failed mint produced no token, so there is
    nothing in either stream to leak by folding them."""
    monkeypatch.setattr(
        luci_auth.subprocess,
        "run",
        lambda cmd, **kw: _completed(
            stdout="partial", stderr="not logged in", returncode=1
        ),
    )

    with pytest.raises(subprocess.CalledProcessError) as e:
        luci_auth.mint_token()

    assert "not logged in" in e.value.output
    assert "partial" in e.value.output


def test_a_warning_on_stderr_does_not_reach_the_token(tmp_path, monkeypatch):
    """The bug this function exists to not have. luci-auth warns whenever it
    refreshes a token it cannot cache -- every call, in a sandbox that mounts
    ~/.config/chrome_infra read-only. Merged into stdout, that warning rode into
    an Authorization header, and the client's "Illegal header value" quoted the
    whole thing: the token in an error message, a log, and a transcript.

    A REAL subprocess writing to both streams, because the merge is a property
    of the kwargs the OS acts on -- a stubbed `subprocess.run` returns whatever
    the stub was told to and passes whether or not the bug is present.
    """
    fake = tmp_path / "luci-auth"
    fake.write_text(
        "#!/bin/sh\n"
        "echo 'WARNING: failed to write token cache: read-only file system' >&2\n"
        "echo ya29.token\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))

    assert luci_auth.mint_token() == "ya29.token"


def test_a_token_with_whitespace_is_refused_without_quoting_it(monkeypatch):
    """Belt to the separation's braces, and the reason it is here rather than at
    the header: this message can omit the value, and the client's cannot."""
    monkeypatch.setattr(
        luci_auth.subprocess,
        "run",
        lambda cmd, **kw: _completed(stdout="WARNING: something\nya29.token\n"),
    )

    with pytest.raises(ValueError) as e:
        luci_auth.mint_token()

    assert "ya29.token" not in str(e.value)
    assert "whitespace" in str(e.value)
