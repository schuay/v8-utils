# v8-utils

CLI and MCP tools for [V8](https://v8.dev/) JavaScript engine developers,
focused on [Pinpoint](https://pinpoint-dot-chromeperf.appspot.com/) performance
infrastructure.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- [luci-auth](https://chromium.googlesource.com/infra/luci/luci-go/+/refs/heads/main/auth/client/cmd/luci-auth/)
  on `$PATH` (required for job creation; not needed for read-only operations)

## Installation

```bash
uv tool install git+https://github.com/schuay/v8-utils.git
```

This installs two commands into an isolated environment:

- **`pp`** — Pinpoint CLI for interactive use
- **`v8-mcp`** — MCP server for use with AI assistants (Claude, Gemini, etc.)

Upgrade later with:

```bash
uv tool upgrade v8-utils
```

## Configuration

Create `~/.config/v8-utils/config.toml` to set defaults:

```toml
# Your Chromium email address. Used as the default user for job listing.
user = "you@chromium.org"

# Google Chat incoming webhook URL for job completion notifications (optional).
# Create one via: Space settings → Apps & integrations → Add webhooks.
chat_webhook = "https://chat.googleapis.com/v1/spaces/.../messages?key=..."

# How often the notification daemon polls for job status updates (default: 60).
poll_interval = 60
```

## CLI (`pp`)

### Listing and inspecting jobs

```bash
# List your 20 most recent jobs
pp list-jobs

# Show details of a specific job
pp show-job https://pinpoint-dot-chromeperf.appspot.com/job/12d17bdff10000

# Show base-vs-experiment comparison table (significant results only)
pp show-results https://pinpoint-dot-chromeperf.appspot.com/job/12d17bdff10000

# Show all results, including non-significant ones
pp show-results --show-all https://pinpoint-dot-chromeperf.appspot.com/job/12d17bdff10000

# Dump raw per-run values as JSON (for further analysis)
pp get-raw-values https://pinpoint-dot-chromeperf.appspot.com/job/12d17bdff10000
```

### Creating jobs

Requires `luci-auth login -scopes https://www.googleapis.com/auth/userinfo.email`.

```bash
# Minimal: benchmark alias, config alias, experiment patch
pp create-job -b js3 -c macm4 --exp-patch crrev/c/12345

# With V8 flags comparison
pp create-job -b js3 -c linux --exp-js-flags "--turbofan --no-sparkplug"

# Full options
pp create-job \
  --benchmark jetstream-main.crossbench \
  --configuration mac-m4-mini-perf \
  --story JetStream \
  --base-git-hash abc1234 \
  --exp-patch https://chromium-review.googlesource.com/c/v8/v8/+/12345 \
  --repeat 100 \
  --bug-id 987654321

# Create and immediately start watching for completion
pp create-job -b js3 -c macm4 --exp-patch crrev/c/12345 --watch
```

**Benchmark aliases:**

| Alias | Benchmark | Default story |
|-------|-----------|---------------|
| `js3` | `jetstream-main.crossbench` | `JetStream` |

**Configuration aliases:**

| Alias | Bot configuration |
|-------|------------------|
| `linux` | `linux-r350-perf` |
| `macm4`  | `mac-m4-mini-perf` |

### Job notifications

The notification daemon polls watched jobs in the background and sends a Google
Chat message (if `chat_webhook` is configured) when each job completes.

```bash
# Watch a job (starts the daemon automatically if not running)
pp watch https://pinpoint-dot-chromeperf.appspot.com/job/12d17bdff10000

# Follow the daemon log to see polling activity
pp logs --follow

# Stop the daemon
pp daemon-stop
```

The daemon persists across terminal sessions. It logs to
`~/.local/share/v8-utils/daemon.log`.

## MCP server (`v8-mcp`)

The MCP server exposes the same Pinpoint tools to AI assistants. Add it to
your MCP client configuration, for example in `~/.gemini/settings.json`:

```json
{
  "mcpServers": {
    "v8-utils": {
      "command": "v8-mcp"
    }
  }
}
```

### Available tools

| Tool | Description |
|------|-------------|
| `pinpoint_show_job` | Fetch key details of a Pinpoint job |
| `pinpoint_list_jobs` | List recent jobs for a user, excluding CQ jobs |
| `pinpoint_show_results` | Base-vs-experiment comparison table with significance testing |
| `pinpoint_get_raw_values` | Per-run raw measurement values (for custom analysis) |
| `pinpoint_create_job` | Create a new Pinpoint A/B try job |
