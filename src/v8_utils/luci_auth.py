"""Minting user OAuth tokens via luci-auth.

A single entry point on purpose: the credential-helper suppression below has to
hold for every caller, and a call site that shells out to luci-auth directly
would quietly lose it.
"""

from __future__ import annotations

import os
import subprocess

# Corp workstations point LUCI_AUTH_CREDENTIAL_HELPER at an SSO helper, and
# luci-auth then mints tokens under that helper's OAuth client id rather than
# its own.  Chromeperf allowlists client ids (catapult's
# dashboard/dashboard/api/api_auth.py) and answers one it does not know with
# 403 "User authentication error", which reads as a permission problem rather
# than a credential one.  Empty disables the helper, sending luci-auth back to
# the credentials cached by `luci-auth login` -- which every caller here
# already names in its own error message.
_CREDENTIAL_HELPER = "LUCI_AUTH_CREDENTIAL_HELPER"


def mint_token() -> str:
    """Return a user OAuth token from luci-auth.

    Raises CalledProcessError, with luci-auth's own diagnostics folded into
    stdout, or FileNotFoundError; callers word those for their own surface.
    """
    return subprocess.check_output(
        ["luci-auth", "token"],
        stderr=subprocess.STDOUT,
        text=True,
        env={**os.environ, _CREDENTIAL_HELPER: ""},
    ).strip()
