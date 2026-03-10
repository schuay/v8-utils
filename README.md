# v8-utils

CLI and MCP tools for [V8](https://v8.dev/) JavaScript engine developers,
focused on [Pinpoint](https://pinpoint-dot-chromeperf.appspot.com/) performance
infrastructure.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- [luci-auth](https://chromium.googlesource.com/infra/luci/luci-go/+/refs/heads/main/auth/client/cmd/luci-auth/)
  on `$PATH` (required for job creation; not needed for read-only operations)
- `gcloud auth application-default login` *(optional — only needed for `--use-cas`)*

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

# How often the notification daemon polls for job status updates (default: 60).
poll_interval = 60

# ── Google Chat notifications (Chat app, primary) ─────────────────────────────
# Service account email associated with the v8-utils Chat app (set by admin).
chat_service_account_email = "v8-utils-pinpoint@your-project.iam.gserviceaccount.com"

# Your personal DM space with the bot. Written automatically by `pp chat-setup`.
# chat_app_space = "spaces/..."

# ── Google Chat notifications (webhook, fallback) ─────────────────────────────
# Create one via: Space settings → Apps & integrations → Add webhooks.
# chat_webhook = "https://chat.googleapis.com/v1/spaces/.../messages?key=..."
```

## CLI (`pp`)

### Listing and inspecting jobs

```bash
# List your 20 most recent jobs
pp list-jobs

# Show details of one or more jobs
pp show-job https://pinpoint-dot-chromeperf.appspot.com/job/12d17bdff10000
pp show-job <url1> <url2> ...

# Show base-vs-experiment comparison table (significant results only)
pp show-results https://pinpoint-dot-chromeperf.appspot.com/job/12d17bdff10000
pp show-results <url1> <url2> ...

# Show all results, including non-significant ones
pp show-results --show-all https://pinpoint-dot-chromeperf.appspot.com/job/12d17bdff10000

# Richer JetStream results from CAS (Score, First, Average, Worst4 per story)
# Requires: gcloud auth application-default login
pp show-results --use-cas https://pinpoint-dot-chromeperf.appspot.com/job/12d17bdff10000
```

### Creating jobs

Requires `luci-auth login -scopes https://www.googleapis.com/auth/userinfo.email`.

```bash
# Simplest: all defaults (js3+sp3 on m1, exp-patch from current branch's CL)
pp create-job

# Override bot and/or templates
pp create-job -c m4
pp create-job -t js3 -c m1 m4

# Multiple configs and templates → creates all combinations (6 jobs)
pp create-job -t js3 js2 sp3 -c linux m1 --exp-patch crrev/c/12345

# Multiple experiment patches → one job per patch
pp create-job -t js3 -c m1 --exp-patch crrev/c/111 crrev/c/222

# Multiple experiment flag sets → one job per flag set
pp create-job -t js3 -c linux --exp-js-flags "--turbofan" "--maglev"

# All combinable: 2 templates × 2 configs × 2 patches = 8 jobs
pp create-job -t js3 sp3 -c linux m1 --exp-patch crrev/c/111 crrev/c/222

# Create and immediately watch all jobs
pp create-job -w

# Full options (using explicit benchmark instead of template)
pp create-job \
  --benchmark jetstream-main.crossbench \
  --configuration mac-m4-mini-perf \
  --story JetStream \
  --base-git-hash abc1234 \
  --exp-patch https://chromium-review.googlesource.com/c/v8/v8/+/12345 \
  --repeat 100 \
  --bug-id 987654321
```

**Templates** (`-t`):

| Template | Benchmark | Default story |
|----------|-----------|---------------|
| `js3` | `jetstream-main.crossbench` | `JetStream` |
| `js2` | `jetstream2.crossbench` | `JetStream2` |
| `sp3` | `speedometer3.crossbench` | `Speedometer3` |

**Configuration aliases** (`-c`):

| Alias | Bot configuration |
|-------|------------------|
| `linux` | `linux-r350-perf` |
| `m1` | `mac-m1_mini_2020-perf` |
| `m3` | `mac-m3-pro-perf` |
| `m4` | `mac-m4-mini-perf` |
| `macm4` | `mac-m4-mini-perf` (legacy) |

### Job notifications

The notification daemon polls watched jobs in the background and sends a Google
Chat message when each job completes, including job details and a summary of
significant performance changes.

```bash
# Watch one or more jobs (starts the daemon automatically if not running)
pp watch https://pinpoint-dot-chromeperf.appspot.com/job/12d17bdff10000
pp watch <url1> <url2> ...

# Create jobs and immediately watch them
pp create-job -t js3 js2 -c macm4 --exp-patch crrev/c/12345 --watch

# Follow the daemon log
pp logs --follow

# Stop the daemon
pp daemon-stop
```

The daemon persists across terminal sessions and logs to
`~/.local/share/v8-utils/daemon.log`.

#### Setting up Chat notifications (Chat app, primary)

Notifications are delivered via a shared Google Chat app using service account
impersonation — no key files required. There is a one-time admin setup and a
per-user setup.

**Admin setup (once):**

1. Create a GCP project and enable the **Google Chat API**.
2. Create a service account (e.g. `v8-utils-pinpoint@your-project.iam.gserviceaccount.com`).
3. Publish a Chat app backed by that service account (GCP Console → Google Chat API → Configuration). Set the app display name to something recognisable, e.g. `v8-utils-pinpoint`.
4. Grant each user the **Service Account Token Creator** role on the service account:
   ```bash
   gcloud iam service-accounts add-iam-policy-binding \
     v8-utils-pinpoint@your-project.iam.gserviceaccount.com \
     --member='user:you@google.com' \
     --role='roles/iam.serviceAccountTokenCreator'
   ```
5. Set `chat_service_account_email` in a shared `config.toml` that users copy, or document it for users to add manually.

**Per-user setup:**

1. Log in with Application Default Credentials:
   ```bash
   gcloud auth application-default login
   ```
2. In Google Chat, search for the app by its display name (e.g. `v8-utils-pinpoint`), open a DM, and send it any message.
3. Run the setup command:
   ```bash
   pp chat-setup
   ```
   This identifies your Google account, finds the DM space with the bot, writes
   `chat_app_space` to your config, and sends a confirmation message to Chat.
4. If the notification daemon was already running, restart it:
   ```bash
   pp daemon-stop && pp watch <job_url>
   ```

#### Setting up Chat notifications (webhook, fallback)

As a simpler alternative (no GCP setup), configure an incoming webhook:

```toml
chat_webhook = "https://chat.googleapis.com/v1/spaces/.../messages?key=..."
```

Create the webhook via: Space settings → Apps & integrations → Add webhooks.

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
| `pinpoint_show_results` | Base-vs-experiment comparison table; `use_cas=True` for richer JetStream metrics |
| `pinpoint_create_job` | Create a new Pinpoint A/B try job |
