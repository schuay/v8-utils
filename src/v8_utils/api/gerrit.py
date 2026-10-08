"""Gerrit code review on the Google-operated review hosts: listing changes,
reading and writing comments, and resolving patchsets to git refs.

Results are frozen dataclasses. GerritReader binds trusted author domains to
every read without changing other callers. The context configuration functions
remain for command-line compatibility. With either form, untrusted comment
authors and text, subjects and emails are replaced by the REDACTED_*
placeholders, and unlanded content by untrusted accounts is refused.
"""

from dataclasses import dataclass

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
    UntrustedGerritContent,
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


@dataclass(frozen=True)
class GerritReader:
    """Gerrit reads bound to one trusted-author policy.

    The policy is activated only around each call. Separate readers can run in
    concurrent threads or tasks without changing one another or the unbound CLI
    functions exported by this module.
    """

    trusted_author_domains: tuple[str, ...]

    def __init__(self, trusted_author_domains):
        object.__setattr__(
            self,
            "trusted_author_domains",
            normalize_domains(trusted_author_domains),
        )

    def run(self, fn, /, *args, **kwargs):
        """Run a Gerrit-dependent operation under this reader's policy."""
        from ..trust import use

        with use(self.trusted_author_domains):
            return fn(*args, **kwargs)

    def comments(self, change_url: str, *, include_drafts: bool = False):
        return self.run(comments, change_url, include_drafts=include_drafts)

    def fetch_ref(self, change_url: str, repo_path: str = ".", fetch: bool = True):
        return self.run(fetch_ref, change_url, repo_path=repo_path, fetch=fetch)

    def list_cls(self, query: str, limit: int = 25):
        return self.run(list_cls, query, limit=limit)

    def open_cls(self, query: str, limit: int = 50):
        return self.run(open_cls, query, limit=limit)

    def resolve_patchset(self, change_url: str):
        return self.run(resolve_patchset, change_url)

    def cq_report(self, *args, **kwargs):
        from ..cq import cq_report

        return self.run(cq_report, *args, **kwargs)

    def fetch_gerrit_subject(self, patch_url: str):
        from ..pinpoint import fetch_gerrit_subject

        return self.run(fetch_gerrit_subject, patch_url)

    def subject_or_none(self, patch_url: str | None):
        from ..pinpoint import subject_or_none

        return self.run(subject_or_none, patch_url)


def credentials_available() -> bool:
    """Whether a Gerrit access token can be obtained right now, for the
    configured `gerrit` identity (v8_utils.api.identity)."""
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
    "GerritReader",
    "LabelFlags",
    "OpenChange",
    "Patchset",
    "Range",
    "ReviewResult",
    "UntrustedGerritContent",
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
