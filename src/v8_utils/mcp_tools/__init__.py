"""MCP tool registration for v8-utils, split into opt-in/opt-out groups.

Each group is a module under this package exporting a ``register(mcp)`` function
that defines and decorates its tools. ``build_server`` constructs a FastMCP and
registers the enabled groups; ``GROUPS`` is the authoritative table of group
names, the module that implements each, its default-on/off state, and the
v8-utils-core extra that supplies its dependencies (None for groups that need
only the base install).

Group modules are imported lazily inside ``build_server`` rather than at package
import time: the v8-utils-core distribution puts the heavy scientific/cloud
dependencies behind extras, so an install without them must still start the
server with the groups it can satisfy -- repo_git, worktree, gerrit -- and skip
the rest with an actionable warning instead of failing to import. The v8-utils
distribution installs every extra's dependencies, so no group is ever skipped
there.
"""

import importlib
import logging
from typing import NamedTuple

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.func_metadata import ArgModelBase

# Reject unknown tool parameters instead of silently ignoring them.
ArgModelBase.model_config["extra"] = "forbid"

# REPOS_LINE lives in repo_git, a base group with no heavy deps, so importing it
# eagerly for the server instructions string costs nothing extra.
from .repo_git import REPOS_LINE


_log = logging.getLogger(__name__)


class Group(NamedTuple):
    module: str  # submodule under v8_utils.mcp_tools exporting register(mcp)
    default: bool  # registered unless overridden
    extra: str | None  # pip extra supplying its deps; None means base install


GROUPS: dict[str, Group] = {
    "gerrit": Group("gerrit", True, None),
    "pd": Group("pd", False, "analysis"),
    "performance": Group("performance", True, "analysis"),
    "pinpoint": Group("pinpoint", True, "pinpoint"),
    "repo_git": Group("repo_git", True, None),
    "worktree": Group("worktree", False, None),
}


def build_server(
    overrides: dict[str, bool] | None = None,
    *,
    gerrit_drafts: bool = True,
    default_user: bool = True,
) -> FastMCP:
    """Construct a FastMCP server with the chosen groups registered.

    overrides:     optional {group_name: enabled} mapping. Groups not mentioned
                   fall back to their default in GROUPS.
    gerrit_drafts: when False, gerrit_comments cannot read unpublished drafts
                   and drops the include_drafts parameter (for deployments that
                   expose the tools to untrusted callers).
    default_user:  when False, tools never fall back to the logged-in account:
                   pinpoint job listings require an explicit user and
                   gerrit_list_cls rejects 'self'.
    """
    overrides = overrides or {}
    mcp = FastMCP(
        "v8-utils",
        log_level="WARNING",
        instructions=(
            f"V8 engine development toolkit. Configured repos: {REPOS_LINE}.\n"
            "\n"
            "v8 work often happens in git worktrees. When working on a specific "
            "worktree or branch checkout, call repo_git_worktree_select first: "
            "repo_git_* tools read the main checkout until you do.\n"
            "\n"
            "Tools: "
            "repo_git_* (search/read companion repos), "
            "run_d8 (execute JS in V8 shell), "
            "worktree (create/remove V8 git worktrees), "
            "perf_* (Linux perf profiles), "
            "godbolt_* (Compiler Explorer), "
            "llvm_mca (assembly throughput), "
            "d8_trace_index (V8 traces), "
            "v8log_analyze (V8 logs: deopts, ICs, maps, profile), "
            "jsb_run_bench (run/compare JS benchmarks), "
            "pd_* (perf data: change-point detection and AB compare), "
            "pinpoint_* (Chromium Pinpoint A/B jobs), "
            "gerrit_* (Chromium Gerrit code review)."
        ),
    )
    for name, group in GROUPS.items():
        if not overrides.get(name, group.default):
            continue
        try:
            module = importlib.import_module(f".{group.module}", __package__)
        except ImportError as exc:
            # A group whose optional extra is not installed is skipped, not fatal:
            # a v8-utils-core install intentionally omits the heavy deps. Name the
            # fix so the operator can restore the group if they want it.
            hint = (
                f"install v8-utils-core[{group.extra}]"
                if group.extra
                else "the base install looks incomplete; reinstall v8-utils"
            )
            _log.warning("skipping MCP tool group %r: %s (%s)", name, exc, hint)
            continue
        if name == "gerrit":
            module.register(
                mcp, drafts_enabled=gerrit_drafts, default_user=default_user
            )
        elif name == "pinpoint":
            module.register(mcp, default_user=default_user)
        else:
            module.register(mcp)
    return mcp
