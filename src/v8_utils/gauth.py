"""Google credentials for v8-utils, built from explicit inputs only.

v8-utils also runs inside other processes, and google-auth reads its settings
from process-wide environment variables. A host that sets them for its own
clients changes v8-utils' identity and billing as well:
GOOGLE_APPLICATION_CREDENTIALS swaps in the host's credential, and
GOOGLE_CLOUD_QUOTA_PROJECT bills every call to the host's project, which may
not have the API enabled.

credentials() therefore reads the user's gcloud ADC file directly, ignoring
GOOGLE_APPLICATION_CREDENTIALS, and sets the quota project to what the caller
passes or the file declares, never to the environment's value. Setup is
unchanged: `gcloud auth application-default login`.

A host that holds its Google identity some other way (a machine without a
gcloud login, where the identity comes from a platform token exchange) installs
a source provider with set_source_provider(). credentials() then asks the
provider instead of reading the file. The provider is process-global and set
once by the host from its own configuration; nothing here reads it from the
environment, so the same rule holds: the identity is what the host chose
explicitly, not what the process environment happens to contain.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from google.auth.credentials import Credentials

LOGIN_HINT = "gcloud auth application-default login"

#: Builds the source credential for the given scopes. None means the gcloud
#: ADC file is used.
SourceProvider = Callable[["list[str] | None"], "Credentials"]

_source_provider: SourceProvider | None = None


def set_source_provider(provider: SourceProvider | None) -> None:
    """Install (or with None remove) the source credential provider.

    The provider receives the requested scopes and returns a
    google.auth.credentials.Credentials. credentials() applies the quota
    project to what it returns, as it does to the file credential.
    """
    global _source_provider
    _source_provider = provider


def source_provider() -> SourceProvider | None:
    """The installed provider, or None when the gcloud ADC file is the source."""
    return _source_provider


def adc_path() -> Path:
    """The gcloud ADC file. CLOUDSDK_CONFIG is gcloud's own config-dir
    override and moves the file with it, so it is honoured."""
    root = os.environ.get("CLOUDSDK_CONFIG")
    base = Path(root) if root else Path.home() / ".config" / "gcloud"
    return base / "application_default_credentials.json"


def credentials(scopes: list[str] | None = None, quota_project: str | None = None):
    """User credentials from the installed source provider or the gcloud ADC file.

    quota_project: project to bill API usage to. None uses the file's own
    quota_project_id, which is usually absent: calls then bill to the
    credential's OAuth client, as they do from a clean shell. With a provider
    there is no file; None leaves the quota project unset.

    Raises FileNotFoundError naming the login command when the file is missing,
    and ValueError for a file that does not hold user credentials.
    """
    if _source_provider is not None:
        creds = _source_provider(scopes)
        return creds.with_quota_project(quota_project) if quota_project else creds

    from google.oauth2.credentials import Credentials

    path = adc_path()
    try:
        info = json.loads(path.read_text())
    except FileNotFoundError:
        raise FileNotFoundError(
            f"no application default credentials at {path}; run: {LOGIN_HINT}"
        ) from None
    # The login command writes authorized_user. The type-specific loader is
    # used because the generic one is deprecated, and it reads no environment.
    if info.get("type") != "authorized_user":
        raise ValueError(
            f"{path} holds {info.get('type')!r} credentials, expected"
            f" 'authorized_user'; run: {LOGIN_HINT}"
        )
    creds = Credentials.from_authorized_user_info(info, scopes=scopes)
    return creds.with_quota_project(quota_project or info.get("quota_project_id"))
