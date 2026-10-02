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
uv tool install v8-utils
# Or install directly from GitHub:
uv tool install git+https://github.com/schuay/v8-utils.git
# Upgrade:
uv tool upgrade v8-utils
```

All CLIs (`pp`, `vt`, `jsb`, `pd`, `lv`, `v8-mcp`) support `--version`.
See [RELEASING.md](RELEASING.md) for release preparation and publishing.

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

### Update prompts

Interactive `pp` and `vt` commands check GitHub's `main/VERSION` at most once
per day. A larger integer than the installed stamp prompts `Update from GitHub? [y/N]`.
Accepting runs:

```sh
uv tool install git+https://github.com/schuay/v8-utils.git --reinstall
```

After installation, rerun your command. Declining continues the command and
suppresses checks for a day. Check failures silently continue; installation
failures exit with an error and are not retried for the same stamp. Retry
explicitly with `pp upgrade`.

Set `auto_update = false` at the top level of `~/.config/v8-utils/config.toml`
(or the file selected by `V8_UTILS_CONFIG`) to disable checks. For a one-off
skip, use `V8_UTILS_NO_AUTO_UPDATE=1 pp ...` (also works for `vt`). Manual
`pp upgrade` remains available regardless of these settings.

Checks apply to the full distribution installed by `uv tool` from PyPI or the
upstream Git repository's default branch or `main`. Accepting a prompt on a PyPI
install switches it to GitHub. Local/editable installs, pinned Git revisions,
forks, and `v8-utils-core` are skipped. Noninteractive commands,
`--help`, and `vt create --json` do not check. MCP integration is deferred.

To promote an update, increase the repository's root `VERSION` integer in the
same commit as the change. The wheel bundles this file; the package metadata
version is also 1984 for the initial PyPI release. Installation fetches the latest default branch,
including changes since the stamp was bumped. Existing installs need one
manual upgrade to acquire the checker. To roll back to a known commit:

```sh
V8_UTILS_NO_AUTO_UPDATE=1 uv tool install "git+https://github.com/schuay/v8-utils.git@<commit-sha>" --reinstall
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
