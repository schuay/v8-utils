"""Source lines and inline chains for native code, from DWARF via
llvm-symbolizer's JSON output.

llvm-symbolizer reads a dSYM but does not follow a Mach-O binary's debug map
into its object files, so code without a dSYM gets no lines here.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

_BUNDLED = "third_party/llvm-build/Release+Asserts/bin/llvm-symbolizer"


@dataclass(frozen=True)
class InlineFrame:
    function: str
    # Where the address is in this function: for an inline parent, the call
    # into the next inner function. "" and 0 when DWARF has none.
    file: str
    line: int
    # Where the function starts; "" and 0 when unknown.
    start_file: str
    start_line: int


def find_symbolizer(roots: list[Path]) -> str | None:
    """llvm-symbolizer from a V8 or Chromium checkout's llvm-build, else PATH.
    Xcode does not ship one."""
    for root in roots:
        bundled = root / _BUNDLED
        if bundled.is_file():
            return str(bundled)
    return shutil.which("llvm-symbolizer")


def symbolize(
    symbolizer: str, dwarf_file: Path, addresses: list[int]
) -> dict[int, tuple[InlineFrame, ...]]:
    """The inline chain at each link-time address, innermost first. An
    address DWARF knows nothing about maps to ()."""
    if not addresses:
        return {}
    r = subprocess.run(
        [symbolizer, f"--obj={dwarf_file}", "--output-style=JSON", "--inlines"],
        input="".join(f"{a:#x}\n" for a in addresses),
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(f"llvm-symbolizer failed: {r.stderr.strip()[:500]}")
    chains = {}
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        frames = tuple(
            InlineFrame(
                s["FunctionName"],
                _normpath(s["FileName"]),
                s["Line"],
                _normpath(s.get("StartFileName", "")),
                s["StartLine"],
            )
            for s in entry.get("Symbol", [])
        )
        # Without line info, llvm-symbolizer still names an address from the
        # symbol table; that adds nothing to nm.
        chains[int(entry["Address"], 16)] = (
            frames if any(f.file for f in frames) else ()
        )
    return chains


def _normpath(path: str) -> str:
    # Build directories record paths like out/release/../../src/heap/x.h.
    return os.path.normpath(path) if path else ""
