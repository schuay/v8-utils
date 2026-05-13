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
    ) -> CallToolResult:
        """Manage V8 git worktrees with automatic gclient dependency symlinking.

        Worktrees are created as siblings of the main V8 checkout (e.g. name="foo"
        creates ~/src/v8/foo). gclient-managed dependencies (build/, buildtools/,
        third_party/*, etc.) are symlinked from the main checkout — no gclient sync
        needed. To update shared deps, run gclient sync in the main checkout.

        action:   "create", "remove", or "list"
        name:     worktree directory name (required for create/remove)
        force:    force removal of dirty worktrees (remove only)
        branch:   branch to check out (create only, optional).
                  If it exists, checks it out. Otherwise creates a new branch.
                  Defaults to the worktree name.
        upstream: base branch/ref for the new branch (default "main")
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

        if action == "remove":
            worktree_mod.remove(repo, name, force=force)
            return _text_result(f"Worktree '{name}' removed.")

        raise ValueError(
            f"Unknown action {action!r}. Use 'create', 'remove', or 'list'."
        )
