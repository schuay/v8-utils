"""Which account v8-utils acts as, per use.

A process that runs v8-utils on someone's behalf (a review daemon posting as a
bot) calls `configure` once at startup; a shell user never does, and every use
stays on the host luci-auth login. See v8_utils.identity for the model.
"""

from ..identity import (
    USES,
    GerritAccount,
    IdentityError,
    Impersonate,
    LuciAuth,
    configure,
    describe,
    gerrit_account,
    gerrit_email,
    principal,
    reset,
    token,
    try_token,
)


def verify_gerrit() -> GerritAccount:
    """Resolve the `gerrit` identity against Gerrit and return the account.

    Raises IdentityError when no token can be minted, or when the principal
    declares a `gerrit_account` and /accounts/self resolves to another one. Run
    at startup by anything that posts, so a mis-pointed identity fails there
    instead of posting as the wrong account. The result is remembered: the
    trust layer treats the address as the process's own, and `gerrit_email`
    answers from it.
    """
    from .. import gerrit as _gerrit
    from ..identity import remember_gerrit_account

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
    if not isinstance(account_id, int) or not isinstance(me.get("email"), str):
        raise IdentityError(f"gerrit: /accounts/self returned no usable account: {me}")
    account = GerritAccount(
        email=me["email"], name=str(me.get("name") or ""), account_id=account_id
    )
    remember_gerrit_account(account)
    return account


__all__ = [
    "USES",
    "GerritAccount",
    "IdentityError",
    "Impersonate",
    "LuciAuth",
    "configure",
    "describe",
    "gerrit_account",
    "gerrit_email",
    "principal",
    "reset",
    "token",
    "try_token",
    "verify_gerrit",
]
