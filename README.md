# v8-utils

CLI and MCP tools for [V8](https://v8.dev/) JavaScript engine developers.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- [luci-auth](https://chromium.googlesource.com/infra/luci/luci-go/+/refs/heads/main/auth/client/cmd/luci-auth/) on `$PATH` (for Pinpoint job creation)
- `gcloud auth application-default login` (optional, for CAS data access)

## Installation

```bash
# Everything -- all CLIs and all MCP tool groups:
uv tool install "v8-utils @ git+https://github.com/schuay/v8-utils.git"
# Upgrade:
uv tool upgrade v8-utils
```

There are no extras to remember: a forgotten one breaks a console script
outright (the CLIs import their dependencies at module scope) and silently drops
MCP tool groups, so the `v8-utils` distribution installs the full stack.

### Subsetting the install

Deployments that want only part of the surface install the companion
`v8-utils-core` distribution from `packaging/core/` instead. Same code, same
entry points, but the scientific/cloud stack sits behind extras that the MCP
server loads lazily; groups whose extra is missing are skipped at startup with a
warning naming the extra:

```bash
# MCP server core plus the git-backed tool groups only:
uv pip install "git+https://github.com/schuay/v8-utils.git#subdirectory=packaging/core"
# ... plus a subset:
uv pip install "v8-utils-core[analysis] @ git+https://github.com/schuay/v8-utils.git#subdirectory=packaging/core"
```

| Extra | Enables |
|-------|---------|
| (none) | `repo_git_*`, worktree and `gerrit_*` MCP groups |
| `analysis` | the `pd` and `performance` MCP groups, the `pd` / `jsb` CLIs (numpy/pandas/scipy/ruptures) |
| `pinpoint` | the `pinpoint` MCP group and the `pp` CLI |
| `gchat` | the Google Chat frontend (`pp` daemon) |
| `spanner` | the Spanner-backed perf timeseries adaptor |
| `all` | everything -- equivalent to the `v8-utils` distribution |

Both distributions install the same `v8_utils` module, so they are alternatives
rather than layers: an environment gets one or the other.

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
