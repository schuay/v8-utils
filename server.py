"""MCP server exposing tools useful for V8 JavaScript engine developers.

Run directly:  python server.py
Or via MCP CLI: mcp run server.py

Note the server may be upgraded via: uv tool upgrade v8-mcp
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("v8-mcp")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
