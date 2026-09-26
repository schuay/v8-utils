"""Commit-queue results for a Gerrit CL, read from Buildbucket through the bb
CLI: an overview of which builders passed and failed, or one builder's failed
steps with their cleaned logs."""

import re as _re
import shutil
import subprocess

from .concurrency import _run_concurrent
from .paging import paginate_result


def _bb_run(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    """Run a bb CLI command, raising ValueError on missing binary or auth."""
    bb = shutil.which("bb")
    if bb is None:
        raise ValueError(
            "bb (Buildbucket CLI) not found. "
            "Install depot_tools and ensure it is on PATH."
        )
    r = subprocess.run([bb, *args], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        stderr = r.stderr.strip()
        if "Login required" in stderr or "not logged in" in stderr:
            raise ValueError(f"bb auth required: run 'bb auth-login'.\n{stderr}")
        if stderr:
            raise ValueError(f"bb {args[0]} failed: {stderr}")
    return r


def _parse_bb_jsonl(stdout: str) -> list[dict]:
    """Parse bb JSONL output (one JSON object per line)."""
    import json

    builds = []
    for line in stdout.strip().splitlines():
        line = line.strip()
        if line:
            try:
                builds.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return builds


def _bb_builder_name(build: dict) -> str:
    """Extract 'project/bucket/builder' from a build dict."""
    b = build.get("builder", {})
    return "/".join(
        p for p in [b.get("project"), b.get("bucket"), b.get("builder")] if p
    )


def _bb_categorize(builds: list[dict]) -> dict[str, list[dict]]:
    """Group builds by status category, deduplicating by builder name.

    When multiple CQ attempts produce builds for the same builder,
    keeps only the latest (highest build id) per builder name per category.
    """
    cats: dict[str, list[dict]] = {
        "SUCCESS": [],
        "FAILURE": [],
        "INFRA_FAILURE": [],
        "RUNNING": [],
        "CANCELED": [],
    }
    for b in builds:
        status = b.get("status", "")
        if status in ("STARTED", "SCHEDULED"):
            cats["RUNNING"].append(b)
        elif status in cats:
            cats[status].append(b)
    # Deduplicate: keep latest build per builder name in each category
    for key in cats:
        seen: dict[str, dict] = {}
        for b in cats[key]:
            name = _bb_builder_name(b)
            if name not in seen or str(b.get("id", "")) > str(seen[name].get("id", "")):
                seen[name] = b
        cats[key] = list(seen.values())
    return cats


def _bb_leaf_failures(build: dict) -> list[str]:
    """Return leaf failed step names from a build (which already has steps)."""
    failed = [s for s in build.get("steps", []) if s.get("status") == "FAILURE"]
    names = {s["name"] for s in failed}
    return [
        s["name"]
        for s in failed
        if not any(o != s["name"] and o.startswith(s["name"] + "|") for o in names)
    ]


def _bb_short_name(build: dict) -> str:
    """Extract just the builder name (without project/bucket prefix)."""
    return build.get("builder", {}).get("builder", _bb_builder_name(build))


def _format_cq_overview(
    cl_number: str,
    patchset: int,
    cats: dict[str, list[dict]],
) -> str:
    """Format CQ results as a compact overview (no logs)."""
    n_pass = len(cats["SUCCESS"])
    n_fail = len(cats["FAILURE"])
    n_infra = len(cats["INFRA_FAILURE"])
    n_run = len(cats["RUNNING"])
    n_cancel = len(cats["CANCELED"])
    total = n_pass + n_fail + n_infra + n_run + n_cancel

    parts = []
    if n_pass:
        parts.append(f"{n_pass} passed")
    if n_fail:
        parts.append(f"{n_fail} failed")
    if n_infra:
        parts.append(f"{n_infra} infra failures")
    extra = ""
    if n_run:
        extra += f"; {n_run} running"
    if n_cancel:
        extra += f"; {n_cancel} canceled"

    lines = [
        f"CQ results for {cl_number}/{patchset}",
        "",
        f"Summary: {', '.join(parts)} (of {total} builds{extra})",
    ]

    if cats["RUNNING"]:
        lines.append("")
        lines.append("RUNNING:")
        for b in cats["RUNNING"]:
            lines.append(f"  {_bb_short_name(b)}")

    if cats["INFRA_FAILURE"]:
        lines.append("")
        lines.append("INFRA_FAILURE:")
        for b in cats["INFRA_FAILURE"]:
            sm = b.get("summaryMarkdown", "")
            detail = f"  ({sm[:200]})" if sm else ""
            lines.append(f"  {_bb_short_name(b)}{detail}")

    if cats["FAILURE"]:
        lines.append("")
        lines.append("FAILED:")
        for b in cats["FAILURE"]:
            step_names = _bb_leaf_failures(b)
            if step_names:
                steps_str = ", ".join(step_names[:3])
                if len(step_names) > 3:
                    steps_str += f", +{len(step_names) - 3} more"
                lines.append(f"  {_bb_short_name(b)}  ({steps_str})")
            else:
                lines.append(f"  {_bb_short_name(b)}")

    if n_pass:
        lines.append("")
        lines.append(f"{n_pass} passed (not shown)")

    lines.append("")
    lines.append("Use builder=<name> to zoom into a specific bot's failure logs.")

    return "\n".join(lines)


def _dedup_lines(text: str) -> str:
    """Collapse consecutive duplicate lines, showing count."""
    lines = text.splitlines()
    if not lines:
        return text
    out: list[str] = []
    prev = lines[0]
    count = 1
    for line in lines[1:]:
        if line == prev:
            count += 1
        else:
            out.append(prev if count == 1 else f"{prev}  (x{count})")
            prev = line
            count = 1
    out.append(prev if count == 1 else f"{prev}  (x{count})")
    return "\n".join(out)


_RE_INFRA_LOG = _re.compile(
    r"^\[?[DIW]\d{4}-\d{2}-\d{2}T"
    r"|^I\d{4} "
    r"|^INFO:"
    r"|^swarming_bot_logs:"
    r"|^Use of LUCI "
    r"|^[0-9a-f]{16}: "
)


def _strip_infra(lines: list[str]) -> list[str]:
    """Remove infrastructure log lines everywhere, then trim blank edges."""
    lines = [ln for ln in lines if not _RE_INFRA_LOG.match(ln)]
    # Trim leading/trailing blank lines
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def _clean_log(text: str) -> str:
    """Light cleanup of a build log: dedup lines, strip PASS/infra noise."""
    lines = [ln for ln in text.splitlines() if not ln.rstrip().endswith(": PASS")]
    lines = _strip_infra(lines)
    return _dedup_lines("\n".join(lines))


def _format_cq_builder_detail(
    build: dict,
) -> str:
    """Fetch and format failure logs for a single builder."""
    builder = _bb_short_name(build)
    build_id = str(build.get("id", ""))
    step_names = _bb_leaf_failures(build)

    lines = [f"Failure details for {builder} (build {build_id})", ""]

    if not step_names:
        lines.append("(no failed steps found)")
        return "\n".join(lines)

    def fetch_log(step_name: str) -> tuple[str, str | None]:
        try:
            lr = _bb_run(["log", build_id, step_name, "stdout"], timeout=30)
            return step_name, lr.stdout
        except (ValueError, subprocess.TimeoutExpired):
            return step_name, None

    fns = [lambda s=s: fetch_log(s) for s in step_names]
    results = _run_concurrent(fns)

    for step_name, raw_log in results:
        lines.append(f"── {step_name} ──")
        if raw_log is None:
            lines.append("(log fetch failed or timed out)")
        else:
            lines.append(_clean_log(raw_log))
        lines.append("")

    return "\n".join(lines)


def cq_report(
    change: str,
    patchset: int,
    builder: str = "",
    offset: int = 0,
    limit: int = 200,
) -> str:
    """Show CQ bot results for a Gerrit CL.

    Without builder: returns a compact overview of which bots passed/failed.
    With builder: zooms into that bot's failure logs (with backtraces).
    """
    from .pinpoint_cache import parse_patch_fields

    # Parse CL number from URL or bare number
    _, cl_number, _ = parse_patch_fields(change)
    if not cl_number:
        # Try bare number
        stripped = change.strip().split("/")[0]
        if stripped.isdigit():
            cl_number = stripped
        else:
            return f"Error: cannot parse CL number from {change!r}"

    # bb matches CLs on host, change number and patchset only, so the
    # project segment is omitted rather than guessed; hardcoding one would
    # be silently wrong for CLs outside it.
    cl_spec = f"chromium-review.googlesource.com/c/{cl_number}/{patchset}"

    try:
        r = _bb_run(["ls", "-cl", cl_spec, "-json", "-steps"])
    except ValueError as e:
        return f"Error: {e}"

    builds = _parse_bb_jsonl(r.stdout)
    if not builds:
        return f"No builds found for CL {cl_number} patchset {patchset}."

    cats = _bb_categorize(builds)

    if not builder:
        return _format_cq_overview(cl_number, patchset, cats)

    # Zoom into a specific builder
    matches = [
        b for b in cats["FAILURE"] if builder.lower() in _bb_builder_name(b).lower()
    ]
    if not matches:
        all_failed = [_bb_short_name(b) for b in cats["FAILURE"]]
        return (
            f"No failed builder matching {builder!r}.\n"
            f"Failed builders: {', '.join(all_failed) or '(none)'}"
        )
    if len(matches) > 1:
        names = [_bb_short_name(b) for b in matches]
        return (
            f"Multiple builders match {builder!r}: {', '.join(names)}\n"
            f"Be more specific."
        )

    full = _format_cq_builder_detail(matches[0])
    return paginate_result(full.splitlines(), offset, limit)
