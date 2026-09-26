"""Running the d8 shell on a script, with its output bounded."""

import subprocess
from pathlib import Path

from . import config


_MAX_D8_OUTPUT = 5_000


def run_d8(
    args: list[str],
    d8_path: str | None = None,
    cwd: str | None = None,
    timeout: int = 60,
    output_file: str | None = None,
) -> str:
    """Run the d8 JavaScript shell with the given arguments.

    For benchmarking, use the jsb_run_bench tool instead.

    stdout and stderr are combined into a single stream.

    Example -- run a JetStream3 line item:
      args: ["cli.js", "--", "regexp-octane"]
      cwd:  "/absolute/path/to/JetStream3"
    """
    cfg = config.load()
    if d8_path:
        d8 = Path(d8_path).expanduser()
    else:
        d8 = cfg.v8_out / cfg.default_build / "d8"
    if not d8.exists():
        raise ValueError(f"d8 not found: {d8}")

    cmd = [str(d8), *args]
    stdout = open(output_file, "w") if output_file else subprocess.PIPE
    try:
        result = subprocess.run(
            cmd,
            stdout=stdout,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            errors="replace",
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        return f"Error: d8 timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"
    finally:
        if output_file:
            stdout.close()

    parts: list[str] = []
    if output_file:
        parts.append(f"[output → {output_file}]")
    elif result.stdout:
        parts.append(result.stdout)
    if result.returncode not in (0, 1):
        parts.append(f"[exit {result.returncode}]")

    out = "\n".join(parts).strip()
    if not out:
        out = "(no output)"
    if len(out) > _MAX_D8_OUTPUT:
        out = (
            out[:_MAX_D8_OUTPUT]
            + f"\n\n[truncated — {len(out) - _MAX_D8_OUTPUT:,} more chars. "
            "Use output_file to redirect large output to a file.]"
        )
    return out
