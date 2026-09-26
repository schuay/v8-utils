"""The public surface of v8-utils.

The command-line tools, the MCP server and in-process consumers import these
modules and nothing else from v8_utils (tests/test_layering.py). A function
here may only forward to an internal one; what matters is that the internals
behind it can change without any of those callers noticing.

Import an area by name (`from v8_utils.api import pd`). This package imports
none of them, so pulling in one area never loads another's dependencies.
"""
