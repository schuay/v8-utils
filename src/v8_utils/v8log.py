"""v8log — Parser and analysis for V8's v8.log profiling output.

Parses v8.log files directly (no preprocessing required) and provides
analysis functions for deopts, ICs, maps, tick profiles, and more.
Shared between the ``lv`` CLI and MCP tools.
"""

from __future__ import annotations

import bisect
import logging
import os
import re
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Callable

from rich import box
from rich.console import Console
from rich.table import Table

# ── CSV parsing (ported from tools/csvparser.mjs) ────────────────────────────

_ESCAPE_RE = re.compile(r"\\(x[0-9A-Fa-f]{2}|u[0-9A-Fa-f]{4}|n|\\)")


def _unescape(s: str) -> str:
    """Process V8 log escape sequences: \\x2C, \\xHH, \\uHHHH, \\n, \\\\."""
    if "\\" not in s:
        return s

    def _replace(m: re.Match) -> str:
        seq = m.group(1)
        if seq == "n":
            return "\n"
        if seq == "\\":
            return "\\"
        # \x2C → ',' , \xHH → char, \uHHHH → char
        return chr(int(seq[1:], 16))

    return _ESCAPE_RE.sub(_replace, s)


def _split_line(line: str) -> list[str]:
    """Split a v8.log CSV line and unescape fields."""
    return [_unescape(f) for f in line.split(",")]


# ── Data structures ──────────────────────────────────────────────────────────

_TIER_MAP = {
    "": "compiled",
    "~": "ignition",
    "^": "sparkplug",
    "+": "maglev",
    "+'": "maglev",
    "o+": "maglev",
    "o+'": "maglev",
    "*": "turbofan",
    "*'": "turbofan",
    "o*": "turbofan",
    "o*'": "turbofan",
}

_TIER_MARKER = {
    "compiled": "",
    "ignition": "~",
    "sparkplug": "^",
    "maglev": "+",
    "turbofan": "*",
}

VM_STATES = {
    0: "JS",
    1: "GC",
    2: "PARSER",
    3: "BYTECODE_COMPILER",
    4: "COMPILER",
    5: "OTHER",
    6: "EXTERNAL",
    7: "ATOMICS_WAIT",
    8: "IDLE",
    9: "LOGGING",
    10: "IDLE_EXTERNAL",
}

IC_TYPES = frozenset(
    {
        "LoadIC",
        "StoreIC",
        "KeyedLoadIC",
        "KeyedStoreIC",
        "LoadGlobalIC",
        "StoreGlobalIC",
        "StoreInArrayLiteralIC",
    }
)

_IC_STATE_NAMES = {
    "0": "uninitialized",
    "X": "no_feedback",
    "1": "monomorphic",
    "P": "polymorphic",
    ".": "recompute_handler",
    "N": "megamorphic",
    "G": "generic",
}


def _ic_state_name(s: str) -> str:
    return _IC_STATE_NAMES.get(s, s)


@dataclass
class CodeEntry:
    type: str
    name: str
    start: int
    size: int
    timestamp: int
    state: str = ""

    @property
    def tier(self) -> str:
        return _TIER_MAP.get(self.state, "compiled")

    @property
    def tier_marker(self) -> str:
        return _TIER_MARKER.get(self.tier, "")

    @property
    def func_name(self) -> str:
        if self.type == "CPP":
            return self.name
        idx = self.name.find(" ")
        return self.name[:idx] if idx != -1 else self.name

    @property
    def source(self) -> str:
        if self.type == "CPP":
            return ""
        idx = self.name.find(" ")
        return self.name[idx + 1 :] if idx != -1 else ""


@dataclass
class DeoptEntry:
    timestamp: int
    code_size: int
    instruction_start: int
    inlining_id: int
    script_offset: int
    deopt_kind: str
    deopt_location: str
    deopt_reason: str


@dataclass
class IcEntry:
    ic_type: str
    pc: int
    timestamp: int
    line: int
    column: int
    old_state: str
    new_state: str
    map_id: str
    key: str
    modifier: str
    slow_reason: str


@dataclass
class MapEvent:
    event_type: str
    timestamp: int
    map_id: str = ""
    transition_type: str = ""
    from_id: str = ""
    to_id: str = ""
    reason: str = ""
    name: str = ""
    pc: int = 0
    line: int = 0
    column: int = 0
    details: str = ""


@dataclass
class TickEntry:
    pc: int
    timestamp: int
    vm_state: int
    stack: list[int] = field(default_factory=list)


@dataclass
class SharedLibrary:
    name: str
    start: int
    end: int
    aslr_slide: int


# ── CodeMap ──────────────────────────────────────────────────────────────────


class CodeMap:
    """Maps instruction addresses to CodeEntry objects using sorted ranges."""

    def __init__(self) -> None:
        self._starts: list[int] = []
        self._entries: list[CodeEntry] = []

    def add(self, entry: CodeEntry) -> None:
        idx = bisect.bisect_right(self._starts, entry.start)
        self._starts.insert(idx, entry.start)
        self._entries.insert(idx, entry)

    def move(self, from_addr: int, to_addr: int) -> None:
        idx = bisect.bisect_left(self._starts, from_addr)
        if idx < len(self._starts) and self._starts[idx] == from_addr:
            entry = self._entries[idx]
            del self._starts[idx]
            del self._entries[idx]
            entry.start = to_addr
            self.add(entry)

    def delete(self, addr: int) -> None:
        idx = bisect.bisect_left(self._starts, addr)
        if idx < len(self._starts) and self._starts[idx] == addr:
            del self._starts[idx]
            del self._entries[idx]

    def lookup(self, addr: int) -> CodeEntry | None:
        idx = bisect.bisect_right(self._starts, addr) - 1
        if idx < 0:
            return None
        entry = self._entries[idx]
        if entry.start <= addr < entry.start + entry.size:
            return entry
        return None

    def all_entries(self) -> list[CodeEntry]:
        return list(self._entries)


# ── C++ symbolizer ───────────────────────────────────────────────────────────

_log = logging.getLogger(__name__)

# nm output: "0000000000abcdef 0000000000000123 T symbolName"
# or without size: "0000000000abcdef T symbolName"
_NM_RE = re.compile(r"^([0-9a-fA-F]+)\s+(?:([0-9a-fA-F]+)\s+)?[A-Za-z]\s+(.+)$")


class CppSymbolizer:
    """Lazily resolves C++ symbols from shared libraries via nm."""

    def __init__(self, shared_libs: list[SharedLibrary]) -> None:
        self._libs = shared_libs
        self._loaded: set[str] = set()

    def symbolize_into(self, code_map: CodeMap) -> None:
        """Load C++ symbols from all shared libraries into the code map."""
        for lib in self._libs:
            if lib.name in self._loaded:
                continue
            self._loaded.add(lib.name)
            self._load_lib(lib, code_map)

    def _load_lib(self, lib: SharedLibrary, code_map: CodeMap) -> None:
        path = Path(lib.name)
        if not path.exists():
            _log.debug("Skipping %s: file not found", lib.name)
            return
        try:
            result = subprocess.run(
                ["nm", "-C", "-n", "-S", str(path)],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            _log.debug("nm failed for %s", lib.name)
            return
        if result.returncode != 0:
            _log.debug("nm returned %d for %s", result.returncode, lib.name)
            return

        symbols: list[tuple[int, int, str]] = []
        for line in result.stdout.splitlines():
            m = _NM_RE.match(line)
            if not m:
                continue
            addr = int(m.group(1), 16)
            size = int(m.group(2), 16) if m.group(2) else 0
            name = m.group(3)
            symbols.append((addr, size, name))

        # Fill in missing sizes from gaps between consecutive symbols
        for i in range(len(symbols) - 1):
            if symbols[i][1] == 0:
                gap = symbols[i + 1][0] - symbols[i][0]
                symbols[i] = (symbols[i][0], gap, symbols[i][2])
        if symbols and symbols[-1][1] == 0:
            symbols[-1] = (symbols[-1][0], 1, symbols[-1][2])

        lib_base = lib.start
        lib_end = lib.end
        for nm_addr, size, name in symbols:
            runtime_addr = lib_base + nm_addr
            if runtime_addr < lib_base or runtime_addr >= lib_end:
                continue
            code_map.add(
                CodeEntry(
                    type="CPP",
                    name=name,
                    start=runtime_addr,
                    size=size,
                    timestamp=0,
                    state="",
                )
            )


# ── V8Log parser ─────────────────────────────────────────────────────────────


def _parse_addr(s: str) -> int:
    """Parse an address or size field. Matches JS parseInt() behavior:
    0x-prefixed → hex, otherwise decimal."""
    return int(s, 0)


class V8Log:
    """Parses a v8.log file and provides access to all event streams."""

    def __init__(self) -> None:
        self.code_map = CodeMap()
        self.shared_libs: list[SharedLibrary] = []
        self.deopts: list[DeoptEntry] = []
        self.ics: list[IcEntry] = []
        self.maps: list[MapEvent] = []
        self.ticks: list[TickEntry] = []
        self.scripts: dict[int, tuple[str, str]] = {}
        self._sfi_map: dict[int, int] = {}
        self._symbolized = False

    def symbolize(self) -> None:
        """Load C++ symbols from shared libraries into the code map.

        Lazy: only runs nm on first call, subsequent calls are no-ops.
        """
        if self._symbolized:
            return
        self._symbolized = True
        CppSymbolizer(self.shared_libs).symbolize_into(self.code_map)

    @classmethod
    def parse(
        cls,
        path: Path,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> V8Log:
        log = cls()
        total = os.path.getsize(path)
        consumed = 0
        line_count = 0

        with open(path, "r", errors="replace") as f:
            for raw_line in f:
                consumed += len(raw_line.encode("utf-8", errors="replace"))
                line_count += 1
                if on_progress and line_count % 20000 == 0:
                    on_progress(consumed, total)

                line = raw_line.rstrip("\n\r")
                if not line:
                    continue

                # Fast dispatch: find first comma to get event name
                comma = line.find(",")
                if comma == -1:
                    continue
                event = line[:comma]

                try:
                    if event == "code-creation":
                        log._handle_code_creation(line[comma + 1 :])
                    elif event == "tick":
                        log._handle_tick(line[comma + 1 :])
                    elif event in IC_TYPES:
                        log._handle_ic(event, line[comma + 1 :])
                    elif event == "code-deopt":
                        log._handle_code_deopt(line[comma + 1 :])
                    elif event == "map":
                        log._handle_map(line[comma + 1 :])
                    elif event == "map-create":
                        log._handle_map_create(line[comma + 1 :])
                    elif event == "map-details":
                        log._handle_map_details(line[comma + 1 :])
                    elif event == "code-move":
                        log._handle_code_move(line[comma + 1 :])
                    elif event == "code-delete":
                        log._handle_code_delete(line[comma + 1 :])
                    elif event == "sfi-move":
                        log._handle_sfi_move(line[comma + 1 :])
                    elif event == "shared-library":
                        log._handle_shared_library(line[comma + 1 :])
                    elif event == "script-source":
                        log._handle_script_source(line[comma + 1 :])
                except (ValueError, IndexError):
                    # Skip malformed lines
                    pass

        if on_progress:
            on_progress(total, total)
        return log

    # ── Event handlers ───────────────────────────────────────────────────

    def _handle_code_creation(self, rest: str) -> None:
        fields = _split_line(rest)
        # type, kind, timestamp, start, size, nameAndPosition, [sfi, state]
        if len(fields) < 6:
            return
        code_type = fields[0]
        timestamp = int(fields[2])
        start = _parse_addr(fields[3])
        size = _parse_addr(fields[4])
        name = fields[5]
        state = ""
        if code_type != "RegExp" and len(fields) >= 8:
            state = fields[7]
        entry = CodeEntry(
            type=code_type,
            name=name,
            start=start,
            size=size,
            timestamp=timestamp,
            state=state,
        )
        self.code_map.add(entry)

    def _handle_tick(self, rest: str) -> None:
        fields = _split_line(rest)
        if len(fields) < 5:
            return
        pc = _parse_addr(fields[0])
        timestamp = int(fields[1])
        # fields[2] is is_external_callback (unused for now)
        tos_or_ext = _parse_addr(fields[3]) if fields[3] else 0
        vm_state = int(fields[4])

        # Resolve stack
        stack = [pc]
        if tos_or_ext:
            entry = self.code_map.lookup(tos_or_ext)
            if entry and entry.type in ("LazyCompile", "Script"):
                stack.append(tos_or_ext)

        prev = pc
        for frame in fields[5:]:
            if not frame or frame == "overflow":
                continue
            if frame[0] in ("+", "-"):
                offset = int(frame, 0)
                prev += offset
                stack.append(prev)
            else:
                addr = _parse_addr(frame)
                stack.append(addr)
                prev = addr

        self.ticks.append(
            TickEntry(
                pc=pc,
                timestamp=timestamp,
                vm_state=vm_state,
                stack=stack,
            )
        )

    def _handle_ic(self, ic_type: str, rest: str) -> None:
        fields = _split_line(rest)
        if len(fields) < 10:
            return
        self.ics.append(
            IcEntry(
                ic_type=ic_type,
                pc=_parse_addr(fields[0]),
                timestamp=int(fields[1]),
                line=int(fields[2]),
                column=int(fields[3]),
                old_state=fields[4],
                new_state=fields[5],
                map_id=fields[6],
                key=fields[7],
                modifier=fields[8],
                slow_reason=fields[9],
            )
        )

    def _handle_code_deopt(self, rest: str) -> None:
        fields = _split_line(rest)
        if len(fields) < 8:
            return
        self.deopts.append(
            DeoptEntry(
                timestamp=int(fields[0]),
                code_size=int(fields[1]),
                instruction_start=_parse_addr(fields[2]),
                inlining_id=int(fields[3]),
                script_offset=int(fields[4]),
                deopt_kind=fields[5],
                deopt_location=fields[6],
                deopt_reason=fields[7],
            )
        )

    def _handle_map(self, rest: str) -> None:
        fields = _split_line(rest)
        if len(fields) < 9:
            return
        self.maps.append(
            MapEvent(
                event_type="transition",
                transition_type=fields[0],
                timestamp=int(fields[1]),
                from_id=fields[2],
                to_id=fields[3],
                pc=_parse_addr(fields[4]) if fields[4] else 0,
                line=int(fields[5]) if fields[5] else 0,
                column=int(fields[6]) if fields[6] else 0,
                reason=fields[7],
                name=fields[8],
                map_id=fields[3],
            )
        )

    def _handle_map_create(self, rest: str) -> None:
        fields = _split_line(rest)
        if len(fields) < 2:
            return
        self.maps.append(
            MapEvent(
                event_type="create",
                timestamp=int(fields[0]),
                map_id=fields[1],
            )
        )

    def _handle_map_details(self, rest: str) -> None:
        fields = _split_line(rest)
        if len(fields) < 3:
            return
        self.maps.append(
            MapEvent(
                event_type="details",
                timestamp=int(fields[0]),
                map_id=fields[1],
                details=fields[2],
            )
        )

    def _handle_code_move(self, rest: str) -> None:
        fields = _split_line(rest)
        if len(fields) < 2:
            return
        self.code_map.move(_parse_addr(fields[0]), _parse_addr(fields[1]))

    def _handle_code_delete(self, rest: str) -> None:
        fields = _split_line(rest)
        if not fields:
            return
        self.code_map.delete(_parse_addr(fields[0]))

    def _handle_sfi_move(self, rest: str) -> None:
        fields = _split_line(rest)
        if len(fields) < 2:
            return
        self._sfi_map[_parse_addr(fields[0])] = _parse_addr(fields[1])

    def _handle_shared_library(self, rest: str) -> None:
        fields = _split_line(rest)
        if len(fields) < 4:
            return
        self.shared_libs.append(
            SharedLibrary(
                name=fields[0],
                start=_parse_addr(fields[1]),
                end=_parse_addr(fields[2]),
                aslr_slide=_parse_addr(fields[3]),
            )
        )

    def _handle_script_source(self, rest: str) -> None:
        fields = _split_line(rest)
        if len(fields) < 3:
            return
        self.scripts[int(fields[0])] = (fields[1], fields[2])


# ── Analysis: summary dataclasses ────────────────────────────────────────────


@dataclass
class DeoptSummary:
    total: int
    by_kind: dict[str, int]
    by_reason: list[tuple[str, int]]
    hot_sites: list[tuple[str, str, str, int, str]]  # func, source, kind, count, reason


@dataclass
class IcSummary:
    total: int
    by_state: dict[str, int]
    hot_mega: list[
        tuple[str, str, str, str, int, int]
    ]  # func, source, ic_type, key, count, maps_seen


@dataclass
class MapSummary:
    creates: int
    transitions: int
    details_count: int
    deprecated: list[tuple[str, str]]  # map_id, details
    by_reason: list[tuple[str, int]]
    most_transitioned: list[tuple[str, int, str]]  # map_id, count, name
    details_map: dict[str, str]  # map_id → raw details string


@dataclass
class ProfileEntry:
    self_ticks: int
    self_pct: float
    total_ticks: int
    total_pct: float
    name: str
    source: str
    tier: str
    tier_marker: str


@dataclass
class ProfileSummary:
    total_ticks: int
    vm_state_counts: dict[str, int]
    entries: list[ProfileEntry]


@dataclass
class VmsSummary:
    total: int
    counts: dict[str, int]


@dataclass
class FnSummary:
    pattern: str
    compilations: list[CodeEntry]
    deopts: list[DeoptEntry]
    ics: list[IcEntry]
    self_ticks: int
    total_ticks: int
    total_program_ticks: int
    callers: list[tuple[str, str, int]]  # func_name, source, count


# ── Analysis functions ───────────────────────────────────────────────────────


def analyze_deopts(
    log: V8Log,
    top: int = 20,
    filter_pat: str | None = None,
) -> DeoptSummary:
    deopts = log.deopts
    if filter_pat:
        filtered = []
        for d in deopts:
            entry = log.code_map.lookup(d.instruction_start)
            name = entry.func_name if entry else ""
            if fnmatch(name, filter_pat):
                filtered.append(d)
        deopts = filtered

    by_kind: Counter[str] = Counter()
    by_reason: Counter[str] = Counter()
    site_counts: Counter[tuple[str, str, str, str]] = Counter()

    for d in deopts:
        by_kind[d.deopt_kind] += 1
        by_reason[d.deopt_reason] += 1
        entry = log.code_map.lookup(d.instruction_start)
        func = entry.func_name if entry else f"0x{d.instruction_start:x}"
        source = entry.source if entry else ""
        site_counts[(func, source, d.deopt_kind, d.deopt_reason)] += 1

    hot_sites = [
        (func, source, kind, count, reason)
        for (func, source, kind, reason), count in site_counts.most_common(top)
    ]

    return DeoptSummary(
        total=len(deopts),
        by_kind=dict(by_kind),
        by_reason=by_reason.most_common(top),
        hot_sites=hot_sites,
    )


def analyze_ics(
    log: V8Log,
    top: int = 20,
    filter_pat: str | None = None,
) -> IcSummary:
    ics = log.ics
    if filter_pat:
        filtered = []
        for ic in ics:
            entry = log.code_map.lookup(ic.pc)
            name = entry.func_name if entry else ""
            if fnmatch(name, filter_pat):
                filtered.append(ic)
        ics = filtered

    by_state: Counter[str] = Counter()
    # Group megamorphic/generic sites
    mega_sites: dict[tuple[str, str, str, str], set[str]] = defaultdict(set)
    mega_counts: Counter[tuple[str, str, str, str]] = Counter()

    for ic in ics:
        state_name = _ic_state_name(ic.new_state)
        by_state[state_name] += 1

        if state_name in ("megamorphic", "generic"):
            entry = log.code_map.lookup(ic.pc)
            func = entry.func_name if entry else f"0x{ic.pc:x}"
            source = entry.source if entry else f"{ic.line}:{ic.column}"
            key = (func, source, ic.ic_type, ic.key)
            mega_sites[key].add(ic.map_id)
            mega_counts[key] += 1

    hot_mega = [
        (
            func,
            source,
            ic_type,
            ic_key,
            count,
            len(mega_sites[(func, source, ic_type, ic_key)]),
        )
        for (func, source, ic_type, ic_key), count in mega_counts.most_common(top)
    ]

    return IcSummary(
        total=len(ics),
        by_state=dict(by_state),
        hot_mega=hot_mega,
    )


def analyze_maps(log: V8Log, top: int = 20) -> MapSummary:
    creates = 0
    transitions = 0
    details_count = 0
    by_reason: Counter[str] = Counter()
    transition_counts: Counter[str] = Counter()
    map_names: dict[str, str] = {}
    deprecated: list[tuple[str, str]] = []
    details_map: dict[str, str] = {}

    for m in log.maps:
        if m.event_type == "create":
            creates += 1
        elif m.event_type == "transition":
            transitions += 1
            by_reason[m.reason] += 1 if m.reason else 0
            transition_counts[m.to_id] += 1
            if m.name:
                map_names[m.to_id] = m.name
        elif m.event_type == "details":
            details_count += 1
            details_map[m.map_id] = m.details
            if "deprecated" in m.details.lower():
                deprecated.append((m.map_id, m.details))

    most_transitioned = [
        (map_id, count, map_names.get(map_id, ""))
        for map_id, count in transition_counts.most_common(top)
    ]

    return MapSummary(
        creates=creates,
        transitions=transitions,
        details_count=details_count,
        deprecated=deprecated,
        by_reason=[(r, c) for r, c in by_reason.most_common(top) if r],
        most_transitioned=most_transitioned,
        details_map=details_map,
    )


def analyze_profile(
    log: V8Log,
    top: int = 20,
    filter_pat: str | None = None,
) -> ProfileSummary:
    log.symbolize()
    self_counts: Counter[int | None] = Counter()
    total_counts: Counter[int | None] = Counter()
    vm_counts: Counter[str] = Counter()

    for tick in log.ticks:
        vm_counts[VM_STATES.get(tick.vm_state, "OTHER")] += 1
        # Self: top-of-stack only
        entry = log.code_map.lookup(tick.pc)
        self_counts[entry.start if entry else None] += 1
        # Total: walk the full call stack (stack[0] == pc), count each fn once
        seen: set[int | None] = set()
        for addr in tick.stack:
            ce = log.code_map.lookup(addr)
            k = ce.start if ce else None
            if k not in seen:
                seen.add(k)
                total_counts[k] += 1

    total = len(log.ticks)
    # Rank by self ticks; apply filter only to what we show
    ranked = [
        (addr, count)
        for addr, count in self_counts.most_common()
        if not filter_pat
        or addr is None
        or (
            (e := log.code_map.lookup(addr)) is not None
            and fnmatch(e.func_name, filter_pat)
        )
    ][:top]

    entries: list[ProfileEntry] = []
    for addr, sc in ranked:
        tc = total_counts.get(addr, 0)
        if addr is None:
            entries.append(
                ProfileEntry(
                    self_ticks=sc,
                    self_pct=100.0 * sc / total if total else 0,
                    total_ticks=tc,
                    total_pct=100.0 * tc / total if total else 0,
                    name="(unknown)",
                    source="",
                    tier="",
                    tier_marker="",
                )
            )
        else:
            entry = log.code_map.lookup(addr)
            if entry:
                entries.append(
                    ProfileEntry(
                        self_ticks=sc,
                        self_pct=100.0 * sc / total if total else 0,
                        total_ticks=tc,
                        total_pct=100.0 * tc / total if total else 0,
                        name=entry.func_name,
                        source=entry.source,
                        tier=entry.tier,
                        tier_marker=entry.tier_marker,
                    )
                )

    return ProfileSummary(
        total_ticks=total,
        vm_state_counts=dict(vm_counts),
        entries=entries,
    )


def analyze_vms(log: V8Log) -> VmsSummary:
    counts: Counter[str] = Counter()
    for tick in log.ticks:
        counts[VM_STATES.get(tick.vm_state, "OTHER")] += 1
    return VmsSummary(total=len(log.ticks), counts=dict(counts))


def analyze_fn(log: V8Log, pattern: str) -> FnSummary:
    log.symbolize()
    # Find all matching code entries
    matching: list[CodeEntry] = []
    matching_addrs: set[int] = set()
    for entry in log.code_map.all_entries():
        if fnmatch(entry.func_name, pattern):
            matching.append(entry)
            matching_addrs.add(entry.start)

    # Collect deopts for matching code
    fn_deopts = [d for d in log.deopts if d.instruction_start in matching_addrs]

    # Collect ICs where the PC falls within a matching code entry
    fn_ics = []
    for ic in log.ics:
        entry = log.code_map.lookup(ic.pc)
        if entry and entry.start in matching_addrs:
            fn_ics.append(ic)

    # Count ticks and extract callers
    # tick.stack[0] == tick.pc (self), so walk tick.stack directly
    self_ticks = 0
    total_ticks = 0
    caller_counts: Counter[tuple[str, str]] = Counter()
    for tick in log.ticks:
        for i, addr in enumerate(tick.stack):
            ce = log.code_map.lookup(addr)
            if ce and ce.start in matching_addrs:
                if i == 0:
                    self_ticks += 1
                total_ticks += 1
                # Caller: one level up (what called us)
                if i + 1 < len(tick.stack):
                    caller = log.code_map.lookup(tick.stack[i + 1])
                    if caller:
                        caller_counts[(caller.func_name, caller.source)] += 1
                break

    callers = [
        (name, source, count) for (name, source), count in caller_counts.most_common(10)
    ]

    return FnSummary(
        pattern=pattern,
        compilations=matching,
        deopts=fn_deopts,
        ics=fn_ics,
        self_ticks=self_ticks,
        total_ticks=total_ticks,
        total_program_ticks=len(log.ticks),
        callers=callers,
    )


# ── Formatting ───────────────────────────────────────────────────────────────


def _render_table(table: Table, ansi: bool) -> str:
    console = Console(
        no_color=not ansi,
        highlight=False,
        width=200,
        force_terminal=ansi,
    )
    with console.capture() as capture:
        console.print(table, end="")
    return capture.get()


def _bar(value: float, max_value: float, width: int = 30) -> str:
    if max_value <= 0:
        return ""
    n = int(width * value / max_value)
    return "█" * n


def format_deopts(summary: DeoptSummary, ansi: bool = False) -> str:
    parts: list[str] = []
    kinds = "  ".join(f"{k}: {v}" for k, v in sorted(summary.by_kind.items()))
    parts.append(f"Deopts: {summary.total} total  ({kinds})")

    if summary.by_reason:
        parts.append("")
        parts.append("By reason:")
        max_count = summary.by_reason[0][1] if summary.by_reason else 1
        for reason, count in summary.by_reason:
            bar = _bar(count, max_count, 20)
            parts.append(f"  {reason:<40s} {count:>4d}  {bar}")

    if summary.hot_sites:
        parts.append("")
        table = Table(
            box=box.SIMPLE,
            show_header=True,
            header_style="bold",
            padding=(0, 1),
            title="Hot sites",
        )
        table.add_column("function")
        table.add_column("source")
        table.add_column("kind")
        table.add_column("count", justify="right")
        table.add_column("reason")
        for func, source, kind, count, reason in summary.hot_sites:
            table.add_row(func, source, kind, str(count), reason)
        parts.append(_render_table(table, ansi))

    return "\n".join(parts)


def format_ics(summary: IcSummary, ansi: bool = False) -> str:
    parts: list[str] = []
    parts.append(f"ICs: {summary.total:,} total")

    if summary.by_state:
        max_count = max(summary.by_state.values()) if summary.by_state else 1
        for state in (
            "monomorphic",
            "polymorphic",
            "megamorphic",
            "generic",
            "uninitialized",
            "no_feedback",
            "recompute_handler",
        ):
            count = summary.by_state.get(state, 0)
            if count == 0:
                continue
            pct = 100.0 * count / summary.total if summary.total else 0
            bar = _bar(count, max_count, 20)
            parts.append(f"  {state:<20s} {count:>6,}  {pct:>4.0f}%  {bar}")

    if summary.hot_mega:
        parts.append("")
        table = Table(
            box=box.SIMPLE,
            show_header=True,
            header_style="bold",
            padding=(0, 1),
            title="Hot megamorphic/generic sites",
        )
        table.add_column("function")
        table.add_column("source")
        table.add_column("type")
        table.add_column("key")
        table.add_column("count", justify="right")
        table.add_column("maps seen", justify="right")
        for func, source, ic_type, key, count, maps_seen in summary.hot_mega:
            table.add_row(func, source, ic_type, key, str(count), str(maps_seen))
        parts.append(_render_table(table, ansi))

    return "\n".join(parts)


def format_maps(
    summary: MapSummary,
    ansi: bool = False,
    verbose: bool = False,
) -> str:
    parts: list[str] = []
    parts.append(
        f"Maps created: {summary.creates:,}   "
        f"Transitions: {summary.transitions:,}   "
        f"Deprecated: {len(summary.deprecated)}"
    )

    if summary.by_reason:
        parts.append("")
        parts.append("By reason:")
        max_count = summary.by_reason[0][1] if summary.by_reason else 1
        for reason, count in summary.by_reason:
            bar = _bar(count, max_count, 20)
            parts.append(f"  {reason:<30s} {count:>6,}  {bar}")

    if summary.most_transitioned:
        parts.append("")
        table = Table(
            box=box.SIMPLE,
            show_header=True,
            header_style="bold",
            padding=(0, 1),
            title="Most transitioned maps",
        )
        table.add_column("map")
        table.add_column("transitions", justify="right")
        table.add_column("name")
        for map_id, count, name in summary.most_transitioned:
            table.add_row(map_id, str(count), name)
        parts.append(_render_table(table, ansi))

    if summary.deprecated:
        parts.append("")
        parts.append(f"Deprecated maps: {len(summary.deprecated)}")
        for map_id, details in summary.deprecated[:10]:
            parts.append(f"  {map_id}")
            if verbose:
                for dl in details.split("\n"):
                    parts.append(f"    {dl}")

    if verbose and summary.details_map:
        parts.append("")
        parts.append("Map details:")
        for map_id, details in list(summary.details_map.items())[:20]:
            parts.append(f"  {map_id}:")
            for dl in details.split("\n"):
                parts.append(f"    {dl}")

    return "\n".join(parts)


def format_profile(summary: ProfileSummary, ansi: bool = False) -> str:
    parts: list[str] = []

    # VM state header
    vm_parts = []
    for state in (
        "JS",
        "GC",
        "COMPILER",
        "PARSER",
        "BYTECODE_COMPILER",
        "OTHER",
        "EXTERNAL",
        "IDLE",
    ):
        count = summary.vm_state_counts.get(state, 0)
        if count == 0:
            continue
        pct = 100.0 * count / summary.total_ticks if summary.total_ticks else 0
        vm_parts.append(f"{state} {pct:.0f}%")
    parts.append("VM state: " + "  ".join(vm_parts))
    parts.append("")

    table = Table(
        box=box.SIMPLE,
        show_header=True,
        header_style="bold",
        padding=(0, 1),
    )
    table.add_column("self%", justify="right")
    table.add_column("total%", justify="right")
    table.add_column("ticks", justify="right")
    table.add_column("tier")
    table.add_column("name")
    table.add_column("source")

    for e in summary.entries:
        marker = e.tier_marker if e.tier_marker else " "
        table.add_row(
            f"{e.self_pct:.1f}%",
            f"{e.total_pct:.1f}%",
            str(e.self_ticks),
            marker,
            e.name,
            e.source,
        )
    parts.append(_render_table(table, ansi))
    parts.append("")
    parts.append("(~ ignition  ^ sparkplug  + maglev  * turbofan)")

    return "\n".join(parts)


def format_vms(summary: VmsSummary, ansi: bool = False) -> str:
    parts: list[str] = []
    max_count = max(summary.counts.values()) if summary.counts else 1
    for state in (
        "JS",
        "GC",
        "COMPILER",
        "BYTECODE_COMPILER",
        "PARSER",
        "OTHER",
        "EXTERNAL",
        "IDLE",
        "ATOMICS_WAIT",
        "LOGGING",
        "IDLE_EXTERNAL",
    ):
        count = summary.counts.get(state, 0)
        if count == 0:
            continue
        pct = 100.0 * count / summary.total if summary.total else 0
        bar = _bar(count, max_count, 30)
        parts.append(f"  {state:<20s} {pct:>5.1f}%  {bar}")
    return "\n".join(parts)


def format_fn(summary: FnSummary, ansi: bool = False) -> str:
    parts: list[str] = []

    if not summary.compilations:
        parts.append(f"No code entries matching '{summary.pattern}'")
        return "\n".join(parts)

    # Header: use the first compilation's name
    first = summary.compilations[0]
    parts.append(f"{first.func_name}  ({first.source})")
    parts.append("")

    # Compilations
    tiers = [e.tier for e in summary.compilations]
    tier_str = " → ".join(tiers)
    latest_tier = tiers[-1] if tiers else "?"
    parts.append(f"Tier:     {latest_tier}  ({tier_str})")

    # Ticks
    total = summary.total_program_ticks
    if total:
        self_pct = 100.0 * summary.self_ticks / total
        total_pct = 100.0 * summary.total_ticks / total
        parts.append(f"Hot:      self {self_pct:.1f}%  total {total_pct:.1f}%")
    parts.append("")

    # Deopts
    if summary.deopts:
        by_reason: Counter[str] = Counter()
        by_kind: Counter[str] = Counter()
        for d in summary.deopts:
            by_reason[d.deopt_reason] += 1
            by_kind[d.deopt_kind] += 1
        kinds = " / ".join(f"{k}" for k, _ in by_kind.most_common())
        reasons = " / ".join(f"{r}" for r, _ in by_reason.most_common(3))
        parts.append(f"Deopts:   {len(summary.deopts)}×  {kinds} / {reasons}")
        for d in summary.deopts[:10]:
            parts.append(f"  → {d.deopt_location}  ({d.deopt_kind}: {d.deopt_reason})")
        parts.append("")

    # ICs
    if summary.ics:
        table = Table(
            box=box.SIMPLE,
            show_header=True,
            header_style="bold",
            padding=(0, 1),
            title="ICs",
        )
        table.add_column("line:col")
        table.add_column("type")
        table.add_column("state")
        table.add_column("key")

        ic_groups: dict[tuple[int, int, str, str], list[IcEntry]] = defaultdict(list)
        for ic in summary.ics:
            ic_groups[(ic.line, ic.column, ic.ic_type, ic.key)].append(ic)

        for (line, col, ic_type, key), group in sorted(ic_groups.items()):
            final_state = _ic_state_name(group[-1].new_state)
            maps_seen = len({ic.map_id for ic in group})
            state_str = final_state
            if maps_seen > 1:
                state_str += f"  maps: {maps_seen}"
            table.add_row(f"{line}:{col}", ic_type, state_str, key)
        parts.append(_render_table(table, ansi))

    # Callers
    if summary.callers:
        parts.append("")
        parts.append("Callers:")
        for name, source, count in summary.callers:
            src = f"  ({source})" if source else ""
            parts.append(f"  {name}{src}  ×{count:,}")

    return "\n".join(parts)
