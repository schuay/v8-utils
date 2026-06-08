"""vt — V8 worktree manager CLI.

Usage:
  vt list
  vt create <name> [-b BRANCH] [-u UPSTREAM]
  vt remove <name> [-f]
  vt refresh <name>
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from rich import box
from rich.console import Console
from rich.table import Table

from . import config, worktree

console = Console(highlight=False)


def _cmd_list(repo: Path) -> None:
    wts = worktree.list_worktrees(repo)
    if not wts:
        console.print("[dim]No worktrees.[/]")
        return
    table = Table(box=box.SIMPLE, show_header=True, header_style="bold", padding=(0, 1))
    table.add_column("name", no_wrap=True)
    table.add_column("branch", no_wrap=True)
    table.add_column("head")
    for wt in wts:
        path = Path(wt["path"])
        branch = wt.get("branch", "")
        name = path.name
        # Dim the branch when it matches the name (the common case).
        branch_style = "dim" if branch == name else ""
        table.add_row(
            name,
            f"[{branch_style}]{branch}[/]" if branch_style else branch,
            wt.get("head", ""),
        )
    console.print(table)


def _cmd_create(repo: Path, name: str, branch: str | None, upstream: str) -> None:
    result = worktree.create(repo, name, branch, upstream=upstream)
    console.print(f"[bold green]Created[/] {result['path']}")
    for line in result["builds"]:
        console.print(f"  {line.strip()}")


def _cmd_remove(repo: Path, name: str, force: bool, remove_branch: bool) -> bool:
    try:
        result = worktree.remove(repo, name, force=force, remove_branch=remove_branch)
    except Exception as e:
        msg = str(e)
        if isinstance(e, subprocess.CalledProcessError):
            msg = e.stderr.strip() or e.stdout.strip() or msg
        console.print(f"[bold red]Error[/] removing '{name}': {msg}")
        return False
    console.print(f"[bold green]Removed[/] '{name}'")
    if remove_branch:
        if result["branch_removed"]:
            console.print(f"  [dim]branch[/] {result['branch']} [dim]deleted[/]")
        else:
            note = result["note"] or "no branch to delete"
            console.print(f"  [dim]branch kept:[/] {note}")
    return True


def _cmd_refresh(repo: Path, name: str) -> None:
    result = worktree.refresh(repo, name)
    linked = result["linked"]
    console.print(f"[bold green]Refreshed[/] symlinks for '{name}'")
    if linked:
        console.print(f"  [dim]linked {len(linked)} dep(s):[/] {', '.join(linked)}")
    else:
        console.print("  [dim]no deps to link (already up to date)[/]")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="vt", description="V8 worktree manager")
    sub = p.add_subparsers(dest="action", required=True)

    # list
    sub.add_parser("list", aliases=["ls"], help="List worktrees")

    # create
    cp = sub.add_parser("create", aliases=["new"], help="Create a worktree")
    cp.add_argument("name", help="Worktree directory name")
    cp.add_argument("-b", "--branch", help="Branch to check out (default: name)")
    cp.add_argument(
        "-u",
        "--upstream",
        default="main",
        help="Base ref for new branch (default: main)",
    )

    # remove
    rp = sub.add_parser("remove", aliases=["rm"], help="Remove worktree(s)")
    rp.add_argument(
        "names", nargs="+", metavar="name", help="Worktree directory name(s)"
    )
    rp.add_argument(
        "-f", "--force", action="store_true", help="Remove even if worktree is dirty"
    )
    rp.add_argument(
        "--remove-branch",
        action="store_true",
        help="Also delete the underlying git branch",
    )

    # refresh
    fp = sub.add_parser(
        "refresh",
        help="Rebuild gclient dep symlinks after a rebase (fixes DEPS-related "
        "build breakage)",
    )
    fp.add_argument("name", help="Worktree directory name")

    args = p.parse_args(argv)
    repo = config.load().repos["v8"].path

    if args.action in ("list", "ls"):
        _cmd_list(repo)
    elif args.action in ("create", "new"):
        _cmd_create(repo, args.name, args.branch, args.upstream)
    elif args.action in ("remove", "rm"):
        failed = sum(
            not _cmd_remove(repo, n, args.force, args.remove_branch) for n in args.names
        )
        if failed:
            sys.exit(1)
    elif args.action == "refresh":
        _cmd_refresh(repo, args.name)
    else:
        p.print_help()
        sys.exit(1)
