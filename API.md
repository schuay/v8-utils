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

List jobs, newest first. Returns up to 50 jobs per page.

**Query parameters:**

| param    | notes                                                              |
|----------|--------------------------------------------------------------------|
| `user`   | filter by user email, e.g. `jkummerow@chromium.org`               |
| `filter` | colon-separated key:value filter, e.g. `status:Completed`, `benchmark:jetstream2` |
| `cursor` | opaque cursor string from `next_cursor` for pagination             |

**Response:**

| field         | notes                                            |
|---------------|--------------------------------------------------|
| `jobs`        | list of job objects (same schema as `/job/{id}`) |
| `count`       | total matching jobs (capped at 1000)             |
| `max_count`   | same                                             |
| `next_cursor` | pass as `cursor=` to fetch next page             |
| `prev_cursor` | pass as `cursor=` to fetch previous page         |
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
