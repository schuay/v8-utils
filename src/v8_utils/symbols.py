"""Native symbol tables read with nm."""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger(__name__)

# "0000000000abcdef 0000000000000123 T name", or without the size column.
_NM_RE = re.compile(r"^([0-9a-fA-F]+)\s+(?:([0-9a-fA-F]+)\s+)?([A-Za-z])\s+(.+)$")


@dataclass(frozen=True)
class Symbol:
    address: int  # as nm reports it: link-time on Mach-O, file-relative on ELF
    size: int
    name: str


def symbol_file(path: Path) -> Path:
    """`path`, or the DWARF file of its dSYM bundle when there is one.
    Stripped Mach-O binaries keep their symbols there."""
    dsym = path.with_name(path.name + ".dSYM") / "Contents/Resources/DWARF"
    dsym /= path.name
    return dsym if dsym.exists() else path


def read_symbols(path: Path, *, text_only: bool = False) -> list[Symbol] | None:
    """The demangled symbols nm lists for `path`, by address, or None when nm
    fails. Sizes nm leaves out (all of them on Mach-O) are filled in from the
    gap to the next symbol."""
    try:
        result = subprocess.run(
            ["nm", "-C", "-n", "-S", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        _log.debug("nm failed for %s", path)
        return None
    if result.returncode != 0:
        _log.debug("nm returned %d for %s", result.returncode, path)
        return None

    entries: list[tuple[int, int, str]] = []
    for line in result.stdout.splitlines():
        m = _NM_RE.match(line)
        if not m or (text_only and m.group(3) not in "tT"):
            continue
        size = int(m.group(2), 16) if m.group(2) else 0
        entries.append((int(m.group(1), 16), size, m.group(4)))

    symbols = []
    for i, (address, size, name) in enumerate(entries):
        if size == 0:
            size = entries[i + 1][0] - address if i + 1 < len(entries) else 1
        symbols.append(Symbol(address, size, name))
    return symbols
