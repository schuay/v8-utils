"""Whose Gerrit content may reach a reader.

Anyone with a Gerrit account can comment on a public CL, upload one, and choose
its subject; their email address is theirs to choose too. Text from an
arbitrary account is untrusted input for a model that reads it. Once trusted
domains are configured -- the MCP server's --trusted-author-domains, or
configure() in-process -- every Gerrit read path applies the same rule:

- landed content is trusted whoever wrote it: the landed patchset of a
  merged CL (its diff, commit message, file paths, CQ logs) passed human
  review, which is what vouches for it;
- comments are never landed content: text and author written by an account
  outside the domains are replaced with fixed placeholders on every CL, the
  comment keeping its place;
- account emails outside the domains are replaced wherever they are shown;
- unlanded content by an account outside the domains is not shown: an open
  CL whose owner or any patchset uploader is outside them is not read at all,
  and a merged CL's other patchsets need a trusted uploader, since they never
  passed review.

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
REDACTED_PATH = "<file path from an unreviewed patchset omitted>"

# Paths Gerrit generates rather than takes from a commit (Patch.isMagic).
MAGIC_PATHS = frozenset({"/COMMIT_MSG", "/MERGE_LIST", "/PATCHSET_LEVEL"})

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


def landed_patchset(change: dict) -> int | None:
    """The patchset number that landed, for a merged CL; None otherwise.

    Gerrit makes the landed commit the current revision of a MERGED change
    (on a rebasing submit, a patchset the CQ uploads). A merged change whose
    current revision is not in the payload is treated as not landed.
    """
    if change.get("status") != "MERGED":
        return None
    revisions = change.get("revisions")
    current = change.get("current_revision")
    if not isinstance(revisions, dict) or current not in revisions:
        return None
    number = (revisions[current] or {}).get("_number")
    return number if isinstance(number, int) else None


def _uploaders(revision: object) -> list:
    revision = revision if isinstance(revision, dict) else {}
    accounts = [revision.get("uploader")]
    if "real_uploader" in revision:
        accounts.append(revision.get("real_uploader"))
    return accounts


def untrusted_change_reason(change: dict) -> str | None:
    """Why a CL's content is untrusted, None when it may be shown.

    `change` is a ChangeInfo fetched with DETAILED_ACCOUNTS and ALL_REVISIONS
    (or CURRENT_REVISION for a listing), so the owner and the uploaders carry
    their email. A merged CL is trusted: what landed passed review. An open CL
    needs a trusted owner and a trusted uploader for every patchset in the
    payload; one with no revisions in the payload is untrusted, as its
    uploaders cannot be checked. This answers for the CL as a whole -- its
    subject, its existence in a listing; a single patchset's content is
    untrusted_patchset_reason's question.
    """
    if _domains is None or landed_patchset(change) is not None:
        return None
    accounts = [("owner", change.get("owner"))]
    revisions = change.get("revisions")
    if not isinstance(revisions, dict) or not revisions:
        return "its patchset uploaders are not known"
    for rev in revisions.values():
        accounts += [("patchset uploader", a) for a in _uploaders(rev)]
    for role, account in accounts:
        if not email_in_domains(account_email(account), _domains):
            return f"its {role} is outside the trusted author domains"
    return None


def untrusted_patchset_reason(change: dict, patchset: object) -> str | None:
    """Why one patchset's content (diff, file paths, CQ logs) is untrusted,
    None when it may be shown.

    The landed patchset of a merged CL is trusted whoever uploaded it. Any
    other patchset of a merged CL never passed review and needs a trusted
    uploader. A patchset of an open CL is trusted only when the whole CL is.
    `change` must carry ALL_REVISIONS.
    """
    if _domains is None:
        return None
    landed = landed_patchset(change)
    if landed is None:
        return untrusted_change_reason(change)
    number = int(patchset) if str(patchset).isdigit() else None
    if number == landed:
        return None
    revisions = change.get("revisions") or {}
    revision = next(
        (
            r
            for r in revisions.values()
            if isinstance(r, dict) and r.get("_number") == number
        ),
        None,
    )
    if revision is None:
        return f"patchset {patchset} is not known"
    if not all(
        email_in_domains(account_email(a), _domains) for a in _uploaders(revision)
    ):
        return (
            f"patchset {patchset} did not land and its uploader is outside the"
            " trusted author domains"
        )
    return None
