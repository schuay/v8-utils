# Pinpoint API

Base URL: `https://pinpoint-dot-chromeperf.appspot.com/api`

---

## GET /job/{job_id}

Fetch details for a single job.

**Query parameters:**

| param | value   | effect                                              |
|-------|---------|-----------------------------------------------------|
| `o`   | `STATE` | Include per-change attempt/execution state          |
| `o`   | `TAGS`  | Include tags dict                                   |

**Response fields:**

| field              | notes                                                         |
|--------------------|---------------------------------------------------------------|
| `job_id`           |                                                               |
| `name`             |                                                               |
| `status`           | `Running`, `Completed`, `Failed`, `Cancelled`, `Queued`       |
| `user`             |                                                               |
| `created`          | ISO timestamp                                                 |
| `updated`          | ISO timestamp                                                 |
| `started_time`     | ISO timestamp                                                 |
| `comparison_mode`  | `try`, `performance`, `bisect`                                |
| `configuration`    | bot config name                                               |
| `difference_count` | number of detected regressions/improvements (null if pending) |
| `exception`        | error message if failed                                       |
| `cancel_reason`    |                                                               |
| `bug_id`           |                                                               |
| `batch_id`         |                                                               |
| `bots`             | list of bot hostnames used                                    |
| `results_url`      | relative path, see `/api/results2-serve/` below               |
| `arguments`        | see below                                                     |

**`arguments` fields (try job):**

| field                   | notes                                      |
|-------------------------|--------------------------------------------|
| `comparison_mode`       |                                            |
| `benchmark`             |                                            |
| `story`                 |                                            |
| `configuration`         |                                            |
| `base_git_hash`         |                                            |
| `end_git_hash`          |                                            |
| `base_patch`            | Gerrit URL or empty                        |
| `experiment_patch`      | Gerrit URL or empty                        |
| `base_extra_args`       | extra `--js-flags` / `--extra-browser-args` for base |
| `experiment_extra_args` | same for experiment                        |
| `initial_attempt_count` | number of bot runs per variant             |
| `target`                | build target                               |
| `project`               | `chromium`, `v8`, etc.                     |

**Extra fields with `o=STATE`:**

| field    | notes                                                        |
|----------|--------------------------------------------------------------|
| `metric` | primary metric (empty for try jobs)                          |
| `quests` | list: `["Build", "Test", "Get values"]`                      |
| `state`  | list of 2 items (base, exp), each with `change`, `attempts`, `comparisons`, `result_values` |

---

## GET /jobs

List jobs, newest first. Returns up to 50 jobs per page. **No authentication required.**

**Query parameters:**

| param         | notes                                                                      |
|---------------|----------------------------------------------------------------------------|
| `filter`      | `user={email}` — server-side user filter, e.g. `filter=user=jgruber@chromium.org` |
| `next_cursor` | opaque cursor from previous response's `next_cursor` field                 |

**Filter syntax:**

Filters use `key=value` syntax (not `key:value`). Multiple filters can be combined.
Confirmed working filters:

| filter expression              | effect                              |
|--------------------------------|-------------------------------------|
| `user={email}`                 | jobs by a specific user             |
| `comparison_mode=try`          | try jobs only                       |
| `comparison_mode=performance`  | bisect jobs only                    |
| `configuration={name}`         | jobs on a specific bot config       |

`status=` filters appear to be **ignored** by the server; filter by status client-side.

**Notes:**
- A user may have jobs under both `@google.com` and `@chromium.org` addresses; query both and merge client-side.
- To get the current user's email, call `GET https://www.googleapis.com/oauth2/v3/userinfo` with a LUCI Bearer token (`luci-auth token`).

**Response:**

| field         | notes                                            |
|---------------|--------------------------------------------------|
| `jobs`        | list of job objects (same schema as `/job/{id}`) |
| `count`       | total matching jobs (capped at 1000)             |
| `next_cursor` | pass as `next_cursor=` to fetch the next page    |
| `prev_cursor` | pass as `next_cursor=` to fetch the previous page |
| `next`        | bool — whether a next page exists                |
| `prev`        | bool — whether a previous page exists            |

---

## GET /api/results2-serve/{job_id}

Returns an HTML page. Histogram data is embedded as **NDJSON** inside the
last `<!-- ... -->` HTML comment block. Each line is one JSON object, one of:

- **`GenericSet`** — `{type, guid, values}` — lookup table for diagnostic labels
- **histogram** — `{name, unit, binBoundaries, diagnostics, sampleValues, running, allBins}`

The `diagnostics` dict maps `benchmarks`, `stories`, `labels` to GUIDs resolvable
via the GenericSet table. The `labels` GUID is unique per bot run and shared across
all metrics in that run, making it suitable as a join key (`run_id`).

The `running` array (7 elements): `[count, max, meanlogs, mean, min, sum, variance]`.
For try jobs each histogram entry has exactly one `sampleValue`.

---

## POST /new

Create a new Pinpoint job. Requires authentication (LUCI auth cookie).
Not currently exposed as an MCP tool.
