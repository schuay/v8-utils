"""MCP tool for V8 git worktree management."""

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult

from .. import worktree as worktree_mod
from ._shared import _configured_repo, _text_result


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
        """Create, remove, and repair V8 git worktrees (gclient deps symlinked).

        Manages worktrees on disk only. To read from one, select it with
        repo_git_worktree_select (creating does not select); to see what
        exists, repo_git_worktree_list.

        Create makes a sibling of the main checkout (name="foo" -> ~/src/v8/foo),
        symlinks gclient deps from it (no gclient sync needed), and leaves three
        build-ready dirs -- out/x64.{optdebug,debug,release} -- with
        symbol_level=2 and `gn gen` done, so `autoninja -C out/<build> d8` runs
        straight away. To update shared deps, gclient sync in the main checkout.

        The symlink set is a create-time snapshot. If a build breaks after a
        rebase with missing or dangling dep directories, action="refresh"
        rebuilds it against current DEPS (gclient sync main first if needed).

        action:        "create", "remove", or "refresh"
        name:          worktree directory name (required for all actions)
        force:         force removal of dirty worktrees (remove only)
        remove_branch: also delete the underlying git branch (remove only).
                       Skipped for detached HEAD, main/master, or branches
                       checked out in another worktree.
        branch:        branch to check out (create only, optional).
                       If it exists, checks it out. Otherwise creates a new branch.
                       Defaults to the worktree name.
        upstream:      base branch/ref for the new branch (default "main")
        """
        # Worktree management always targets the main checkout: git worktree
        # add/remove/prune operate on the repo as a whole, and the symlink
        # helpers in v8_utils.worktree resolve the main worktree themselves.
        repo = _configured_repo("v8")

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
            f"Unknown action {action!r}. Use 'create', 'remove', or 'refresh'. "
            f"To list worktrees, use repo_git_worktree_list."
        )
