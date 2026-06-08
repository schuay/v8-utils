"""MCP tool for V8 git worktree management."""

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult

from .. import worktree as worktree_mod
from ._shared import _resolve_repo, _text_result


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def worktree(
        action: str,
        name: str | None = None,
        branch: str | None = None,
        upstream: str = "main",
        force: bool = False,
        remove_branch: bool = False,
    ) -> CallToolResult:
        """Manage V8 git worktrees with automatic gclient dependency symlinking.

        Worktrees are created as siblings of the main V8 checkout (e.g. name="foo"
        creates ~/src/v8/foo). gclient-managed dependencies (build/, buildtools/,
        third_party/*, etc.) are symlinked from the main checkout — no gclient sync
        needed. To update shared deps, run gclient sync in the main checkout.

        Create produces three build-ready directories — out/x64.optdebug, out/x64.debug,
        out/x64.release — each with symbol_level=2 forced and `gn gen` already run,
        so `autoninja -C out/<build> d8` will start building without a gn step.

        The symlink set is a snapshot from create time. After rebasing a worktree
        onto a different main, DEPS may no longer match it (new deps unlinked,
        removed deps left dangling). If a build breaks with missing/unexpected
        dep directories after a rebase, use action="refresh" to rebuild the links.
        Run gclient sync in the main checkout first if main has not synced the
        new deps yet.

        action:        "create", "remove", "refresh", or "list"
        name:          worktree directory name (required for create/remove/refresh)
        force:         force removal of dirty worktrees (remove only)
        remove_branch: also delete the underlying git branch (remove only).
                       Skipped for detached HEAD, main/master, or branches
                       checked out in another worktree.
        branch:        branch to check out (create only, optional).
                       If it exists, checks it out. Otherwise creates a new branch.
                       Defaults to the worktree name.
        upstream:      base branch/ref for the new branch (default "main")
        """
        repo = _resolve_repo("v8")

        if action == "list":
            wts = worktree_mod.list_worktrees(repo)
            if not wts:
                return _text_result("No worktrees found.")
            lines = [f"{'path':<50} {'branch':<30} {'head'}"]
            lines.append("-" * len(lines[0]))
            for wt in wts:
                lines.append(
                    f"{wt['path']:<50} {wt.get('branch', ''):<30} {wt.get('head', '')}"
                )
            return _text_result("\n".join(lines))

        if not name:
            raise ValueError(f"'name' is required for action={action!r}")

        if action == "create":
            result = worktree_mod.create(repo, name, branch, upstream=upstream)
            wt_path = result["path"]
            builds = "\n".join(result["builds"])
            return _text_result(
                f"Worktree created at {wt_path}\n"
                f"\n"
                f"Build directories:\n{builds}\n"
                f"\n"
                f"All commands must use this absolute path, e.g.:\n"
                f"  git -C {wt_path} status\n"
                f"  cd {wt_path} && autoninja -C out/x64.release d8\n"
            )

        if action == "refresh":
            result = worktree_mod.refresh(repo, name)
            linked = result["linked"]
            if linked:
                body = f"Linked {len(linked)} dep(s):\n" + "\n".join(
                    f"  {dep}" for dep in linked
                )
            else:
                body = "No deps to link (already up to date)."
            return _text_result(
                f"Symlinks refreshed for '{name}' at {result['path']}.\n{body}"
            )

        if action == "remove":
            result = worktree_mod.remove(
                repo, name, force=force, remove_branch=remove_branch
            )
            lines = [f"Worktree '{name}' removed."]
            if remove_branch:
                if result["branch_removed"]:
                    lines.append(f"Branch '{result['branch']}' deleted.")
                else:
                    note = result["note"] or "no branch to delete"
                    lines.append(f"Branch kept: {note}.")
            return _text_result(" ".join(lines))

        raise ValueError(
            f"Unknown action {action!r}. Use 'create', 'remove', 'refresh', or 'list'."
        )
