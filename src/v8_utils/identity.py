"""Which account v8-utils acts as, per use.

Every outbound call that carries a credential names a *use* -- `gerrit`,
`pinpoint`, `cas` -- and the use maps to a *principal* configured by the
process. Two kinds of principal exist:

- LuciAuth: the host's luci-auth login. Gerrit tokens come from
  `git-credential-luci` (its own OAuth client, which Gerrit expects), the rest
  from `luci-auth token`. This is the default for every use, so a shell user
  of pp/jsb sees no change.
- Impersonate: a service account, reached by keyless impersonation from the
  gcloud ADC user credential. No key file is involved; the ADC identity needs
  roles/iam.serviceAccountTokenCreator on the account.

Identity is a parameter here and nowhere else. A caller never reads the
environment to decide who it is, which is what keeps a daemon's Gerrit identity
(a bot) separate from the operator identity its builds use (RBE through
luci-auth, outside this module).
"""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Mapping
from dataclasses import dataclass

from . import gauth, luci_auth

GERRIT_SCOPE = "https://www.googleapis.com/auth/gerritcodereview"
EMAIL_SCOPE = "https://www.googleapis.com/auth/userinfo.email"
CLOUD_SCOPE = "https://www.googleapis.com/auth/cloud-platform"

# use -> scopes a token for it must carry. CQ votes are Gerrit writes by the
# `gerrit` principal; there is no separate use for them.
USES: dict[str, tuple[str, ...]] = {
    "gerrit": (GERRIT_SCOPE,),
    "pinpoint": (EMAIL_SCOPE,),
    "cas": (EMAIL_SCOPE,),
}


@dataclass(frozen=True)
class GerritAccount:
    """Who Gerrit says the `gerrit` principal is, from /accounts/self."""

    email: str
    name: str
    account_id: int

    def __str__(self) -> str:
        return f"{self.email} (account {self.account_id})"


class IdentityError(ValueError):
    """A token for a use could not be minted. The message names the fix."""


@dataclass(frozen=True)
class LuciAuth:
    """The host's luci-auth login."""

    def describe(self) -> str:
        return "the host luci-auth login"


@dataclass(frozen=True)
class Impersonate:
    """A service account, impersonated from the gcloud ADC user credential.

    gerrit_account is the Gerrit account id the service account is expected to
    resolve to; `verify_gerrit` compares it against /accounts/self so a
    mis-pointed identity fails at startup instead of posting as the wrong user.
    """

    service_account: str
    gerrit_account: int | None = None

    def __post_init__(self) -> None:
        if "@" not in self.service_account:
            raise ValueError(f"not a service account email: {self.service_account!r}")

    def describe(self) -> str:
        return f"{self.service_account} (impersonated from the gcloud ADC login)"


Principal = LuciAuth | Impersonate

_DEFAULT = LuciAuth()


@dataclass(frozen=True)
class _State:
    principals: Mapping[str, Principal]
    uses: Mapping[str, str]  # use -> principal name


_state = _State(principals={"operator": _DEFAULT}, uses={})
_lock = threading.Lock()
# (service account, scopes) -> live google-auth credentials. Impersonated
# tokens live an hour and google-auth refreshes them in place, so caching the
# credentials object (not the token) is what avoids one IAM round trip per call
# without ever pinning an expired token.
_impersonated: dict[tuple[str, tuple[str, ...]], object] = {}
_last_failure: dict[str, str] = {}
# What /accounts/self answered for the `gerrit` principal, once verified. The
# trust layer treats this address as the process's own, and an uploader stamps
# it as the commit author.
_gerrit_account: GerritAccount | None = None


def configure(principals: Mapping[str, Principal], uses: Mapping[str, str]) -> None:
    """Install the principals and the use -> principal mapping for this process.

    Raises ValueError for a use this module does not know or a mapping to an
    undeclared principal. A use absent from `uses` keeps the default
    (LuciAuth).
    """
    global _state, _gerrit_account
    for use, name in uses.items():
        if use not in USES:
            raise ValueError(f"unknown identity use {use!r}; known: {', '.join(USES)}")
        if name not in principals:
            raise ValueError(
                f"identity use {use!r} names undeclared principal {name!r}"
            )
    for name, p in principals.items():
        if not isinstance(p, (LuciAuth, Impersonate)):
            raise ValueError(
                f"principal {name!r} has unsupported type {type(p).__name__}"
            )
    with _lock:
        _state = _State(principals=dict(principals), uses=dict(uses))
        _impersonated.clear()
        _last_failure.clear()
        _gerrit_account = None


def reset() -> None:
    """Back to the defaults. For tests; a running process never calls it."""
    configure({"operator": _DEFAULT}, {})


def principal(use: str) -> Principal:
    if use not in USES:
        raise ValueError(f"unknown identity use {use!r}; known: {', '.join(USES)}")
    name = _state.uses.get(use)
    return _state.principals[name] if name else _DEFAULT


def describe(use: str) -> str:
    """One line naming who `use` runs as, for a startup log."""
    return principal(use).describe()


def remember_gerrit_account(account: GerritAccount) -> None:
    """Record what Gerrit resolved the `gerrit` principal to (verify_gerrit)."""
    global _gerrit_account
    _gerrit_account = account


def gerrit_account() -> GerritAccount | None:
    """The verified Gerrit account, or None before verification."""
    return _gerrit_account


def gerrit_email() -> str | None:
    """The address Gerrit calls run under, when it is known without a network
    round trip: the verified account, else an impersonated principal's service
    account. None for an unverified host login."""
    if _gerrit_account is not None:
        return _gerrit_account.email
    p = principal("gerrit")
    return p.service_account if isinstance(p, Impersonate) else None


def token(use: str) -> str:
    """A bearer token for `use`. Raises IdentityError naming the fix."""
    p = principal(use)
    scopes = USES[use]
    try:
        if isinstance(p, Impersonate):
            return _impersonated_token(p, scopes)
        return _luci_token(use)
    except IdentityError as e:
        _last_failure[use] = str(e)
        raise
    except Exception as e:  # google-auth raises its own hierarchy; word it once.
        msg = _word_failure(p, e)
        _last_failure[use] = msg
        raise IdentityError(msg) from e


def try_token(use: str) -> str | None:
    """`token`, or None when none can be minted. The reason is kept for
    `unavailable_reason`, so a caller that falls back (an anonymous Gerrit
    read) can still say why when the fallback is not enough."""
    try:
        return token(use)
    except IdentityError:
        return None


def unavailable_reason(use: str) -> str:
    """Why the last `try_token(use)` came back empty, as a line naming the fix."""
    if use in _last_failure:
        return _last_failure[use]
    return f"no token has been requested for {use!r} yet"


# ── luci-auth ─────────────────────────────────────────────────────────────────


def _luci_token(use: str) -> str:
    if use == "gerrit":
        return _git_credential_luci()
    try:
        return luci_auth.mint_token()
    except FileNotFoundError:
        raise IdentityError(
            "luci-auth is not on PATH. It ships with depot_tools; add that checkout"
            " to PATH (a systemd user unit does not source a shell profile)."
        ) from None
    except subprocess.CalledProcessError as e:
        detail = (e.output or "").strip()
        raise IdentityError(
            "luci-auth holds no usable credentials; run `luci-auth login` as the"
            " user the process runs as."
            + (f" luci-auth said: {detail}" if detail else "")
        ) from None


def _git_credential_luci() -> str:
    """A Gerrit token from git-credential-luci.

    Not cached: the helper hands out short-lived tokens and does its own
    refresh, so a process-lifetime cache here would pin an expired one in any
    daemon that outlives it. The helper costs ~15ms against a 30s HTTP timeout.
    """
    try:
        out = subprocess.check_output(
            ["git-credential-luci", "get"],
            input="",
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except FileNotFoundError:
        raise IdentityError(
            "git-credential-luci is not on PATH. It ships with depot_tools; add"
            " that checkout to PATH. In a systemd user unit set it explicitly --"
            " units do not source a shell profile, so a working interactive PATH"
            " says nothing about the daemon's."
        ) from None
    except subprocess.CalledProcessError:
        raise IdentityError(
            "git-credential-luci is installed but holds no credentials for this"
            " account. Run `git-credential-luci login` as the user the process"
            " runs as -- a login under a different account does not carry over."
        ) from None
    for line in out.splitlines():
        if line.startswith("password="):
            return line[len("password=") :]
    raise IdentityError("git-credential-luci returned no token")


# ── impersonation ─────────────────────────────────────────────────────────────


def _impersonated_token(p: Impersonate, scopes: tuple[str, ...]) -> str:
    from google.auth.transport.requests import Request

    key = (p.service_account, scopes)
    with _lock:
        creds = _impersonated.get(key)
        if creds is None:
            from google.auth import impersonated_credentials

            creds = impersonated_credentials.Credentials(
                source_credentials=gauth.credentials(scopes=[CLOUD_SCOPE]),
                target_principal=p.service_account,
                target_scopes=list(scopes),
                lifetime=3600,
            )
            _impersonated[key] = creds
        if not creds.valid:  # type: ignore[attr-defined]
            creds.refresh(Request())  # type: ignore[attr-defined]
        return creds.token  # type: ignore[attr-defined]


def _word_failure(p: Principal, e: Exception) -> str:
    if isinstance(p, Impersonate):
        text = str(e)
        if "getAccessToken" in text or "403" in text:
            return (
                f"cannot impersonate {p.service_account}: the gcloud ADC identity"
                " lacks roles/iam.serviceAccountTokenCreator on it, or the IAM"
                " Credentials API is disabled in its project."
            )
        if isinstance(e, FileNotFoundError):
            return f"cannot impersonate {p.service_account}: {text}"
        return f"cannot impersonate {p.service_account}: {type(e).__name__}: {text}"
    return f"{p.describe()}: {type(e).__name__}: {e}"
