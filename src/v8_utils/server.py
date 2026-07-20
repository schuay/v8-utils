"""MCP server exposing tools useful for V8 JavaScript engine developers.

Run directly:  python -m v8_utils.server
Or via the installed entry point: v8-mcp

Tool groups can be toggled with --enable-<group> / --disable-<group>.
Run `v8-mcp --help` for the full list.

Note the server may be upgraded via: uv tool upgrade v8-utils
"""

import argparse

from .mcp_tools import GROUPS, build_server


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="v8-mcp",
        description=(
            "MCP server for V8 development. "
            "Each tool group can be enabled or disabled independently."
        ),
    )
    for name, group in GROUPS.items():
        flag = name.replace("_", "-")
        state = "on" if group.default else "off"
        parser.add_argument(
            f"--enable-{flag}",
            dest=name,
            action="store_true",
            default=None,
            help=f"enable the {name} tool group (default: {state})",
        )
        parser.add_argument(
            f"--disable-{flag}",
            dest=name,
            action="store_false",
            default=None,
            help=f"disable the {name} tool group (default: {state})",
        )
    parser.add_argument(
        "--no-gerrit-drafts",
        dest="gerrit_drafts",
        action="store_false",
        default=True,
        help=(
            "disable reading unpublished Gerrit draft comments and drop the "
            "include_drafts parameter (for untrusted/shared deployments)"
        ),
    )
    parser.add_argument(
        "--no-default-user",
        dest="default_user",
        action="store_false",
        default=True,
        help=(
            "do not fall back to the logged-in account: pinpoint job listings "
            "require an explicit user and gerrit_list_cls rejects 'self' "
            "(avoids exposing the operator's activity to untrusted callers)"
        ),
    )
    args = parser.parse_args()

    overrides = {n: getattr(args, n) for n in GROUPS if getattr(args, n) is not None}
    build_server(
        overrides,
        gerrit_drafts=args.gerrit_drafts,
        default_user=args.default_user,
    ).run()


if __name__ == "__main__":
    main()
