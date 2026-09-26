"""Gerrit code review on the Google-operated review hosts: listing changes,
reading and writing comments, and resolving patchsets to git refs."""

from .. import gerrit as _gerrit
from ..trust import (
    REDACTED_AUTHOR,
    REDACTED_MESSAGE,
    REDACTED_SUBJECT,
    email_in_domains,
    normalize_domains,
)
from ..trust import configure as configure_trusted_domains
from ..trust import domains as trusted_domains
from ..gerrit import (
    comments,
    create_drafts,
    fetch_ref,
    list_cls,
    open_cls,
    post_review_comments,
    publish_drafts,
    resolve_patchset,
)


def credentials_available() -> bool:
    """Whether a Gerrit access token can be obtained right now."""
    return _gerrit._gerrit_token() is not None


__all__ = [
    "REDACTED_AUTHOR",
    "REDACTED_MESSAGE",
    "REDACTED_SUBJECT",
    "comments",
    "configure_trusted_domains",
    "create_drafts",
    "credentials_available",
    "email_in_domains",
    "fetch_ref",
    "list_cls",
    "normalize_domains",
    "open_cls",
    "post_review_comments",
    "publish_drafts",
    "resolve_patchset",
    "trusted_domains",
]
