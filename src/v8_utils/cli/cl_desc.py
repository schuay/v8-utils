"""Set a Gerrit CL description from a file, after validating it.

  cl-set-desc FILE [--repo DIR] [--dry-run]
  cl-set-desc --install-hook [--settings PATH]

`git cl desc -n` takes the description text itself, not a file name, so a
file name passed there is published as the description. This reads FILE,
rejects text that is not a well-formed commit message for the branch's CL,
pipes it to `git cl desc -n -`, and checks that Gerrit now shows exactly
that text.

Checks: ASCII only; a subject line, a blank line, and a body; no local
paths; exactly one Change-Id line, equal to the CL's Change-Id.

--install-hook adds a Claude Code PreToolUse hook (GUARD) to the settings
file that denies Bash commands setting a description any other way:
`git cl desc -n/--new-description`, `git cl upload -m/--message/
--message-file/--commit-description`, and Gerrit's /message endpoint.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

LOCAL_PATH = re.compile(r"(?<![\w.])(/Users/|/home/|/private/|/tmp/|/var/folders/|~/)")
CHANGE_ID = re.compile(r"^Change-Id: (I[0-9a-f]{40})$", re.MULTILINE)
ISSUE_URL = re.compile(r"\((https://[^)\s]+?)/(\d+)\)")


GUARD_ID = "cl-desc-guard"
# Runs as `python3 -c GUARD` with the hook input on stdin. Self-contained, so
# the settings file does not depend on this checkout being mounted.
GUARD = r"""
import json, re, sys
cmd = json.load(sys.stdin).get("tool_input", {}).get("command", "")
seg = r"[^\n;&|]{0,400}?"
flag = r"(?<![\w-])(%s)(?![\w-])"
pats = [
    r"git\W+cl\W+desc\w*" + seg + flag % "-n|--new-description",
    r"git\W+cl\W+upload" + seg
    + flag % "-m|--message|--message-file|--commit-description",
    r"/changes/[^\s'\"]*/(edit:)?message\b",
]
if any(re.search(p, cmd) for p in pats):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason":
            "Setting a Gerrit CL description directly is blocked: git cl desc -n "
            "takes the text, not a file name, and a mistake publishes it. Write "
            "the full description (with Change-Id) to a file and run "
            "cl-set-desc FILE (--dry-run to preview) from the CL's worktree.",
    }}))
"""


class Invalid(Exception):
    pass


def install_hook(settings: Path) -> bool:
    """Adds or refreshes the guard in `settings`. Returns whether it changed."""
    data = json.loads(settings.read_text()) if settings.exists() else {}
    hook = {
        "type": "command",
        "command": "python3",
        "args": ["-c", GUARD],
        "onFailure": "block",
        "statusMessage": GUARD_ID,
    }
    groups = data.setdefault("hooks", {}).setdefault("PreToolUse", [])
    for group in groups:
        group["hooks"] = [
            h for h in group.get("hooks", []) if h.get("statusMessage") != GUARD_ID
        ]
    groups[:] = [g for g in groups if g["hooks"]]
    groups.append({"matcher": "Bash", "hooks": [hook]})
    text = json.dumps(data, indent=2) + "\n"
    if settings.exists() and settings.read_text() == text:
        return False
    tmp = settings.with_suffix(".json.tmp")
    tmp.write_text(text)
    os.replace(tmp, settings)
    return True


def validate(text: str, change_id: str, env: dict[str, str] | None = None) -> str:
    """Returns the normalized description or raises Invalid."""
    env = os.environ if env is None else env
    text = text.strip("\n") + "\n"
    lines = text.splitlines()
    if not text.isascii():
        raise Invalid("non-ASCII characters")
    if len(lines) < 3 or lines[1] != "":
        raise Invalid("expected a subject line, a blank line, and a body")
    subject = lines[0]
    if not subject.strip() or subject.lstrip().startswith(("/", "~", "$")):
        raise Invalid(f"subject does not look like a CL title: {subject!r}")
    if m := LOCAL_PATH.search(text):
        raise Invalid(f"contains a local path: {m.group(0)!r}")
    for var in ("TMPDIR", "HOME"):
        value = env.get(var, "").rstrip("/")
        if len(value) > 1 and value in text:
            raise Invalid(f"contains ${var}")
    ids = CHANGE_ID.findall(text)
    if len(ids) != 1:
        raise Invalid(f"expected exactly one Change-Id line, found {len(ids)}")
    if ids[0] != change_id:
        raise Invalid(f"Change-Id {ids[0]} is not the CL's {change_id}")
    return text


def _issue(repo: Path) -> tuple[str, str]:
    out = subprocess.run(
        ["git", "cl", "issue"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    m = ISSUE_URL.search(out)
    if not m:
        raise Invalid(f"no CL for the current branch in {repo}")
    return m.group(1), m.group(2)


def _gerrit(url: str):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode().split("\n", 1)[1])


def main(argv: list[str] | None = None) -> int:
    from ..api.update import current_version

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--version", action="version", version=f"%(prog)s {current_version()}"
    )
    ap.add_argument("file", type=Path, nargs="?")
    ap.add_argument("--repo", type=Path, default=Path.cwd())
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--install-hook", action="store_true")
    ap.add_argument(
        "--settings", type=Path, default=Path.home() / ".claude" / "settings.json"
    )
    args = ap.parse_args(argv)
    if args.install_hook:
        changed = install_hook(args.settings)
        print(f"{args.settings}: guard {'installed' if changed else 'up to date'}")
        return 0
    if args.file is None:
        ap.error("FILE is required")
    try:
        if not args.file.is_file():
            raise Invalid(f"{args.file} is not a file")
        host, cl = _issue(args.repo)
        change = _gerrit(f"{host}/changes/{cl}")
        new = validate(args.file.read_text(encoding="utf-8"), change["change_id"])
        old = _gerrit(f"{host}/changes/{cl}/revisions/current/commit")["message"]
        sys.stdout.writelines(
            difflib.unified_diff(
                old.splitlines(True), new.splitlines(True), "current", "new"
            )
        )
        if args.dry_run:
            return 0
        subprocess.run(
            ["git", "cl", "desc", "-n", "-"],
            cwd=args.repo,
            input=new,
            text=True,
            check=True,
        )
        now = _gerrit(f"{host}/changes/{cl}/revisions/current/commit")["message"]
        if now.strip("\n") != new.strip("\n"):
            raise Invalid("Gerrit shows a different description after the update")
        print(f"{cl}: description updated")
        return 0
    except (Invalid, subprocess.CalledProcessError) as e:
        print(f"cl-set-desc: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
