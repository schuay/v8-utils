"""Which account v8-utils acts as, per use.

A process that runs v8-utils on someone's behalf (a review daemon posting as a
bot) calls `configure` once at startup; a shell user never does, and every use
stays on the host luci-auth login. See v8_utils.identity for the model.
"""

from ..identity import (
    USES,
    IdentityError,
    Impersonate,
    LuciAuth,
    configure,
    describe,
    principal,
    reset,
    token,
    try_token,
)


def verify_gerrit() -> str:
    """Resolve the `gerrit` identity against Gerrit and return a line naming it.

    Raises IdentityError when no token can be minted, or when the principal
    declares a `gerrit_account` and /accounts/self resolves to another one. Run
    at startup by anything that posts, so a mis-pointed identity fails there
    instead of posting as the wrong account.
    """
    from .. import gerrit as _gerrit

    p = principal("gerrit")
    tok = token("gerrit")
    try:
        me = _gerrit.account_self(tok)
    except Exception as e:  # httpx errors, a 401 worded by _parse_json
        raise IdentityError(
            f"gerrit as {p.describe()}: /accounts/self failed: {e}"
        ) from e
    account_id = me.get("_account_id")
    expected = getattr(p, "gerrit_account", None)
    if expected is not None and account_id != expected:
        raise IdentityError(
            f"gerrit identity mismatch: configured {p.describe()} expects Gerrit"
            f" account {expected}, but /accounts/self is {account_id}"
            f" ({me.get('email')})"
        )
    return f"gerrit as {me.get('email')} (account {account_id}; {p.describe()})"


__all__ = [
    "USES",
    "IdentityError",
    "Impersonate",
    "LuciAuth",
    "configure",
    "describe",
    "principal",
    "reset",
    "token",
    "try_token",
    "verify_gerrit",
]
