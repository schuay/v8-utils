"""Gerrit code review on the Google-operated review hosts: listing changes,
reading and writing comments, and resolving patchsets to git refs.

Results are frozen dataclasses. With trusted author domains configured
(configure_trusted_domains, or the server's --trusted-author-domains), every
read applies v8_utils.trust: untrusted comment authors and text, subjects and
emails are replaced by the REDACTED_* placeholders, and unlanded content by
untrusted accounts is refused with a ValueError.
"""

from .. import gerrit as _gerrit
from ..gerrit import (
    Attention,
    Change,
    CommentEntry,
    CommentThread,
    DraftResult,
    FetchedRef,
    LabelFlags,
    OpenChange,
    Patchset,
    Range,
    ReviewResult,
    Vote,
    comments,
    create_drafts,
    fetch_ref,
    list_cls,
    open_cls,
    post_review_comments,
    publish_drafts,
    resolve_patchset,
)
from ..trust import (
    REDACTED_AUTHOR,
    REDACTED_MESSAGE,
    REDACTED_PATH,
    REDACTED_SUBJECT,
    email_in_domains,
    normalize_domains,
)
from ..trust import configure as configure_trusted_domains
from ..trust import domains as trusted_domains
from ..trust import reset as reset_trusted_domains


def credentials_available() -> bool:
    """Whether a Gerrit access token can be obtained right now."""
    return _gerrit._gerrit_token() is not None


__all__ = [
    "REDACTED_AUTHOR",
    "REDACTED_MESSAGE",
    "REDACTED_PATH",
    "REDACTED_SUBJECT",
    "Attention",
    "Change",
    "CommentEntry",
    "CommentThread",
    "DraftResult",
    "FetchedRef",
    "LabelFlags",
    "OpenChange",
    "Patchset",
    "Range",
    "ReviewResult",
    "Vote",
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
    "reset_trusted_domains",
    "resolve_patchset",
    "trusted_domains",
]
