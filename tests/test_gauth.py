"""v8_utils.gauth: credentials come from the gcloud ADC file and explicit
arguments, never from host-set GOOGLE_APPLICATION_CREDENTIALS or
GOOGLE_CLOUD_QUOTA_PROJECT."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("google.auth")

from v8_utils import gauth  # noqa: E402


def _user_info(client_id: str, **extra) -> dict:
    return {
        "type": "authorized_user",
        "client_id": client_id,
        "client_secret": "secret",
        "refresh_token": "refresh",
        **extra,
    }


@pytest.fixture
def adc(tmp_path, monkeypatch):
    """A gcloud config dir holding a user ADC file, plus host variables that
    must not reach the result."""
    root = tmp_path / "gcloud"
    root.mkdir()
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(root))
    host = tmp_path / "host_adc.json"
    host.write_text(json.dumps(_user_info("host-client")))
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(host))
    monkeypatch.setenv("GOOGLE_CLOUD_QUOTA_PROJECT", "host-project")

    def write(**extra):
        path = root / "application_default_credentials.json"
        path.write_text(json.dumps(_user_info("user-client", **extra)))

    return write


def test_reads_gcloud_file_not_host_credentials(adc):
    adc()
    creds = gauth.credentials()
    assert creds.client_id == "user-client"
    assert creds.quota_project_id is None


def test_explicit_quota_project(adc):
    adc()
    assert gauth.credentials(quota_project="billing").quota_project_id == "billing"


def test_file_quota_project_used_when_none_passed(adc):
    adc(quota_project_id="from-file")
    assert gauth.credentials().quota_project_id == "from-file"


def test_missing_file_names_login(adc):
    with pytest.raises(FileNotFoundError, match="application-default login"):
        gauth.credentials()


def test_non_user_credentials_rejected(adc, tmp_path):
    root = tmp_path / "gcloud"
    (root / "application_default_credentials.json").write_text(
        json.dumps({"type": "service_account"})
    )
    with pytest.raises(ValueError, match="authorized_user"):
        gauth.credentials()
