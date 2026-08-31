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

    The streams are captured SEPARATELY and merged only on failure. Merging
    them always -- which this did -- put luci-auth's warnings into the returned
    string on the SUCCESS path, and it warns whenever it refreshes a token it
    cannot cache, which is every call in a sandbox that mounts
    ~/.config/chrome_infra read-only. The corrupted value then went into an
    Authorization header, and the "Illegal header value" the client raised
    quoted it: the token landed in an error message, a log, and an agent's
    transcript.
    """
    proc = subprocess.run(
        ["luci-auth", "token"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, _CREDENTIAL_HELPER: ""},
    )
    if proc.returncode != 0:
        # Both streams here, which is what the folded-into-stdout contract
        # promises callers. A nonzero exit means luci-auth minted nothing, so
        # there is no token in either one to fold.
        raise subprocess.CalledProcessError(
            proc.returncode, proc.args, output=proc.stdout + proc.stderr
        )
    token = proc.stdout.strip()
    if not token or any(c.isspace() for c in token):
        # Structural, not defensive: a bearer token has no interior whitespace,
        # so anything that does is luci-auth output this function failed to
        # separate. Refused HERE, where the message can omit the value, rather
        # than at the header where the client quotes what it rejected.
        raise ValueError(
            "luci-auth returned a token with whitespace in it; refusing to use"
            " it as a credential (value withheld -- it may be a real token"
            " concatenated with a warning)"
        )
    return token
