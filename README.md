# v8-utils

CLI and MCP tools for [V8](https://v8.dev/) JavaScript engine developers.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- [luci-auth](https://chromium.googlesource.com/infra/luci/luci-go/+/refs/heads/main/auth/client/cmd/luci-auth/) on `$PATH` (for Pinpoint job creation)
- `gcloud auth application-default login` (optional, for CAS data access)

## Installation

```bash
uv tool install git+https://github.com/schuay/v8-utils.git
# Upgrade:
uv tool upgrade v8-utils
```

## Configuration

Create `~/.config/v8-utils/config.toml`:

```toml
user = "you@chromium.org"
```

Run `pp config` to see all available options.

## CLI tools

- **`pp`** — Pinpoint job management: create, list, inspect, compare results, watch with notifications. Run `pp --help` for usage.
- **`jsb`** — JetStream/Speedometer benchmark runner and result comparison.
- **`pd`** — Performance data analysis: change-point detection and AB comparison.

## MCP server

**`v8-mcp`** exposes tools for use with AI assistants (Claude, Gemini, etc.):

- **Pinpoint** — create/list/inspect jobs, compare results
- **Perf** — hotspot analysis, flamegraphs, annotation, TMA, stat, diff
- **Repository** — git grep/find/log/show across configured repos
- **Gerrit** — fetch CLs and comments
- **Godbolt** — compile C/C++ snippets and inspect assembly, with llvm-mca and optimization remarks
- **d8** — run scripts, trace index for navigating verbose V8 trace output

Add to your MCP client config (e.g. `~/.gemini/settings.json`):

```json
{
  "mcpServers": {
    "v8-utils": {
      "command": "v8-mcp"
    }
  }
}
```
