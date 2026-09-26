"""Whose Gerrit content may reach a reader.

Anyone with a Gerrit account can comment on a public CL, upload one, and choose
its subject; their email address is theirs to choose too. Text from an
arbitrary account is untrusted input for a model that reads it. Once trusted
domains are configured -- the MCP server's --trusted-author-domains, or
configure() in-process -- every Gerrit read path applies the same rule:

- comment text and author written by an account outside the domains are
  replaced with fixed placeholders, the comment keeping its place;
- account emails outside the domains are replaced wherever they are shown;
- a CL whose owner or any patchset uploader is outside the domains is not
  read at all (its comments, diff, CQ logs and subject), because everything
  in it -- file paths, commit message, test output -- is theirs.

Unconfigured, nothing is redacted: the command-line tools a developer runs on
their own behalf read Gerrit as it is.

Trust is decided by email domain, exact or a subdomain of a trusted one, and
fails closed: a missing, malformed or non-ASCII address is untrusted.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

REDACTED_MESSAGE = "<comment text by non-whitelisted author omitted>"
REDACTED_AUTHOR = "<non-whitelisted author>"
REDACTED_SUBJECT = "<subject of a CL by a non-whitelisted author omitted>"

_DOMAIN_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")

_domains: tuple[str, ...] | None = None


def normalize_domains(domains: Iterable[str]) -> tuple[str, ...]:
    """The domains lowercased; raises on an empty list or a non-domain entry,
    since either would leave the gate open or matching nothing."""
    out = tuple(d.strip().lower() for d in domains)
    if not out or any(not d for d in out):
        raise ValueError("trusted author domains must be a non-empty list of domains")
    for d in out:
        if not _DOMAIN_RE.match(d):
            raise ValueError(f"not a domain: {d!r}")
    return out


def configure(domains: Iterable[str]) -> None:
    """Trust only accounts in `domains` (and their subdomains) from now on."""
    global _domains
    _domains = normalize_domains(domains)


def domains() -> tuple[str, ...] | None:
    """The configured domains, or None when redaction is off."""
    return _domains


def email_in_domains(email: object, domains: Iterable[str]) -> bool:
    """Whether `email` is an address in one of `domains` or a subdomain."""
    if not isinstance(email, str):
        return False
    e = email.strip().lower()
    if e.count("@") != 1:
        return False
    local, domain = e.split("@")
    if not local or not _DOMAIN_RE.match(domain):
        return False
    return any(domain == d or domain.endswith("." + d) for d in domains)


def is_trusted(email: object) -> bool:
    """Whether content by `email` may be shown as written. Always true while
    redaction is off."""
    return _domains is None or email_in_domains(email, _domains)


def shown_email(email: object) -> str:
    """`email` if trusted, else the placeholder."""
    return email if is_trusted(email) and isinstance(email, str) else REDACTED_AUTHOR


def account_email(account: object) -> str:
    """The email of a Gerrit AccountInfo, "" when it carries none."""
    if isinstance(account, dict) and isinstance(account.get("email"), str):
        return account["email"]
    return ""


def untrusted_change_reason(change: dict) -> str | None:
    """Why a CL's content is untrusted, None when it is trusted.

    `change` is a ChangeInfo fetched with DETAILED_ACCOUNTS and ALL_REVISIONS,
    so the owner and every patchset's uploader carry their email. A CL with no
    revisions in the payload is untrusted: its uploaders cannot be checked.
    """
    if _domains is None:
        return None
    accounts = [("owner", change.get("owner"))]
    revisions = change.get("revisions")
    if not isinstance(revisions, dict) or not revisions:
        return "its patchset uploaders are not known"
    for rev in revisions.values():
        rev = rev if isinstance(rev, dict) else {}
        accounts.append(("patchset uploader", rev.get("uploader")))
        if "real_uploader" in rev:
            accounts.append(("patchset uploader", rev.get("real_uploader")))
    for role, account in accounts:
        if not email_in_domains(account_email(account), _domains):
            return f"its {role} is outside the trusted author domains"
    return None
