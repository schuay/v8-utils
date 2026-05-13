"""MCP tool registration for v8-utils, split into opt-in/opt-out groups.

Each group module exports a ``register(mcp)`` function that defines and
decorates its tools. ``build_server`` constructs a FastMCP and registers
the enabled groups; ``GROUPS`` is the authoritative list of group names,
their register callables, and their default-on/off state.
"""

from typing import Callable

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.func_metadata import ArgModelBase

# Reject unknown tool parameters instead of silently ignoring them.
ArgModelBase.model_config["extra"] = "forbid"

from . import gerrit, performance, pinpoint, repo_git, worktree
from .repo_git import REPOS_LINE


GROUPS: dict[str, tuple[Callable[[FastMCP], None], bool]] = {
    "gerrit": (gerrit.register, True),
    "performance": (performance.register, True),
    "pinpoint": (pinpoint.register, True),
    "repo_git": (repo_git.register, True),
    "worktree": (worktree.register, False),
}


def build_server(overrides: dict[str, bool] | None = None) -> FastMCP:
    """Construct a FastMCP server with the chosen groups registered.

    overrides: optional {group_name: enabled} mapping. Groups not mentioned
               fall back to their default in GROUPS.
    """
    overrides = overrides or {}
    mcp = FastMCP(
        "v8-utils",
        log_level="WARNING",
        instructions=(
            f"V8 engine development toolkit. Configured repos: {REPOS_LINE}.\n"
            "\n"
            "Tools: "
            "repo_git_* (search/read companion repos), "
            "run_d8 (execute JS in V8 shell), "
            "worktree (manage V8 git worktrees), "
            "perf_* (Linux perf profiles), "
            "godbolt_* (Compiler Explorer), "
            "llvm_mca (assembly throughput), "
            "d8_trace_index (V8 traces), "
            "v8log_analyze (V8 logs: deopts, ICs, maps, profile), "
            "jsb_run_bench (run/compare JS benchmarks), "
            "pinpoint_* (Chromium Pinpoint A/B jobs), "
            "gerrit_* (Chromium Gerrit code review)."
        ),
    )
    for name, (register, default) in GROUPS.items():
        if overrides.get(name, default):
            register(mcp)
    return mcp
