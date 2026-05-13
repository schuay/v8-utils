"""LUCI Token Server: mint service-account tokens via realm-based impersonation.

Re-implements the token exchange that the `cas` CLI performs in Go (see
client/casclient/client.go + auth/internal/luci_ts.go in luci-go).  The
exchange lets a @chromium.org user mint a short-lived OAuth token for a
service account whose realm membership grants them access -- the
mechanism the chrome-swarming RBE instance now requires.

Flow:

  1. Caller holds an OAuth user token (any luci-auth login is enough --
     the Token Server only needs to identify the caller).
  2. POST to luci-token-server.appspot.com/prpc/tokenserver.minter.\
     TokenMinter/MintServiceAccountToken with {service_account, realm,
     scopes}.  The Token Server checks the caller has
     `luci.serviceAccounts.mintToken` in the realm.
  3. Returns a short-lived (<=1h) OAuth token for the SA.

Tokens are cached in-process until ~5 minutes before expiry.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

_TOKEN_SERVER_HOST = "luci-token-server.appspot.com"
_MINT_PATH = "/prpc/tokenserver.minter.TokenMinter/MintServiceAccountToken"
_REFRESH_MARGIN_S = 300  # refresh 5 min before expiry
_MIN_VALIDITY_S = 2100  # ask for >=35 min, matching luci-go default


@dataclass
class _CachedToken:
    token: str
    expiry_ts: float  # unix seconds


_cache: dict[tuple[str, str, tuple[str, ...]], _CachedToken] = {}
_cache_lock = threading.Lock()


def _luci_user_token() -> str:
    """Get the caller's user OAuth token via luci-auth (any default scope)."""
    try:
        return subprocess.check_output(
            ["luci-auth", "token"], stderr=subprocess.STDOUT, text=True
        ).strip()
    except FileNotFoundError:
        raise RuntimeError(
            "luci-auth not found in PATH. Install depot_tools or log in via "
            "`luci-auth login`."
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            "luci-auth failed to mint a user token. Run `luci-auth login`.\n"
            f"luci-auth output:\n{e.output.strip()}"
        )


def _parse_expiry(s: str) -> float:
    """Parse RFC 3339 timestamp returned by pRPC JSON encoding."""
    # Format: "2026-05-11T08:25:00Z" (or with fractional seconds)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    from datetime import datetime

    return datetime.fromisoformat(s).timestamp()


def mint_sa_token(
    service_account: str,
    realm: str,
    scopes: list[str],
) -> str:
    """Mint a short-lived OAuth access token for `service_account` via LUCI.

    Cached in-process; refreshed when within `_REFRESH_MARGIN_S` of expiry.
    """
    key = (service_account, realm, tuple(scopes))
    now = time.time()

    with _cache_lock:
        cached = _cache.get(key)
        if cached and cached.expiry_ts - now > _REFRESH_MARGIN_S:
            return cached.token

    user_token = _luci_user_token()
    body = {
        "tokenKind": "SERVICE_ACCOUNT_TOKEN_ACCESS_TOKEN",
        "serviceAccount": service_account,
        "realm": realm,
        "oauthScope": list(scopes),
        "minValidityDuration": _MIN_VALIDITY_S,
    }
    url = f"https://{_TOKEN_SERVER_HOST}{_MINT_PATH}"
    log.debug("MintServiceAccountToken sa=%s realm=%s", service_account, realm)
    r = httpx.post(
        url,
        headers={
            "Authorization": f"Bearer {user_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json=body,
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(
            f"LUCI Token Server returned {r.status_code} for "
            f"MintServiceAccountToken({service_account}, realm={realm}):\n"
            f"{r.text[:500]}"
        )
    # pRPC JSON response is prefixed with ")]}'\n" XSSI guard.
    text = r.text
    if text.startswith(")]}'\n"):
        text = text[5:]
    import json

    data = json.loads(text)
    token = data["token"]
    expiry_ts = _parse_expiry(data["expiry"])
    with _cache_lock:
        _cache[key] = _CachedToken(token=token, expiry_ts=expiry_ts)
    return token
