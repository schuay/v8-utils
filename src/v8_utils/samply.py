"""samply profiles: loading, stack resolution and frame classification.

`samply record --save-only` writes a Firefox processed profile (gzipped
JSON). Frame addresses are relative to their library: an RVA for native
code, and for V8 JIT code an offset into the jitdump, which samply presents
as one library made of every JIT_CODE_LOAD record's code bytes, concatenated
in file order. JIT frames arrive named from the jitdump; native frames are
unsymbolicated, and are named here with nm on the library (or its dSYM)
whose Mach-O UUID matches the recording.

Leaf addresses are exact PCs; caller addresses are return address - 1.
"""

from __future__ import annotations

import bisect
import gzip
import json
import re
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from . import config, disasm, macho
from .jitdump import CodeLoad, JitDump
from .symbols import Symbol, read_symbols, symbol_file
from .v8log import TIER_BY_MARKER

PROFILE_NAME = "profile.json.gz"

# meta.preprocessedProfileVersion values this reader has been checked against.
SUPPORTED_VERSIONS = frozenset({49})

# jitdump name tags (V8 CODE_TYPE_LIST) by frame kind.
_JS_TAGS = frozenset({"JS", "Script", "Eval"})
_BUILTIN_TAGS = frozenset({"Builtin", "BytecodeHandler", "Handler", "Stub"})
# V8 names regexp code "RegExp.< src: '...' flags: '...'", without a tag, to
# match what pprof demangles.
_REGEXP_PREFIX = "RegExp.<"
# Longest first: "o+" must not match as "" followed by a name.
_MARKERS = sorted(TIER_BY_MARKER, key=len, reverse=True)


@dataclass(frozen=True)
class Frame:
    name: str
    # "js", "builtin", "regexp", "jit" (other JIT code) or "native".
    kind: str
    # V8 tier for "js" frames, as in v8log: ignition, sparkplug, maglev,
    # turbofan or compiled.
    tier: str | None
    lib: str | None
    # Library-relative: an RVA for natives, a jitdump offset for JIT code.
    address: int | None
    # Code object flavour for "js" frames: OSR or context-specialized code.
    osr: bool = False
    specialized: bool = False


@dataclass(frozen=True)
class Sample:
    frames: tuple[Frame, ...]  # leaf first
    time_ms: float
    weight: int
    cpu_us: float


def parse_jit_name(name: str, lib: str | None, address: int | None) -> Frame:
    """Classify a jitdump code name, e.g. "JS:*'foo app.js:3:10"."""
    if name.startswith(_REGEXP_PREFIX):
        return Frame(name, "regexp", None, lib, address)
    tag, sep, rest = name.partition(":")
    if not sep:
        return Frame(name, "jit", None, lib, address)
    if tag in _BUILTIN_TAGS:
        return Frame(rest, "builtin", None, lib, address)
    if tag not in _JS_TAGS:
        return Frame(name, "jit", None, lib, address)
    marker = next(m for m in _MARKERS if rest.startswith(m))
    return Frame(
        rest[len(marker) :].strip(),
        "js",
        TIER_BY_MARKER[marker],
        lib,
        address,
        osr=marker.startswith("o"),
        specialized=marker.endswith("'"),
    )


class LibSymbols:
    """A library's text symbols, looked up by profile RVA."""

    def __init__(self, symbols: list[Symbol], text_vmaddr: int) -> None:
        self._symbols = symbols
        self._addresses = [s.address for s in symbols]
        self.text_vmaddr = text_vmaddr

    def lookup(self, rva: int) -> Symbol | None:
        address = rva + self.text_vmaddr
        i = bisect.bisect_right(self._addresses, address) - 1
        if i < 0 or address >= self._symbols[i].address + self._symbols[i].size:
            return None
        return self._symbols[i]


# Symbol tables by (file, mtime, UUID), shared by every profile this process
# loads.
_lib_cache: dict[tuple[str, float, str], LibSymbols | str] = {}


def lib_symbols(path: Path, uuid: str) -> LibSymbols | str:
    """Symbols for the image with `uuid` in `path`, or why there are none."""
    if not path.exists():
        return f"{path} not found"
    key = (str(path), path.stat().st_mtime, uuid)
    if key not in _lib_cache:
        _lib_cache[key] = _load_lib_symbols(path, uuid)
    return _lib_cache[key]


def _load_lib_symbols(path: Path, uuid: str) -> LibSymbols | str:
    image = next((i for i in macho.images(path) if i.uuid == uuid), None)
    if image is None:
        return f"{path} does not match the recording (UUID {uuid}); rebuilt since?"
    dsym = symbol_file(path)
    if dsym != path and any(i.uuid == uuid for i in macho.images(dsym)):
        symbols = read_symbols(dsym, text_only=True)
    else:
        with macho.thin_file(path, image) as thin:
            symbols = read_symbols(thin, text_only=True)
    if symbols is None:
        return f"nm failed on {path}"
    return LibSymbols([_strip_c_underscore(s) for s in symbols], image.text_vmaddr)


def _strip_c_underscore(symbol: Symbol) -> Symbol:
    # Mach-O prefixes C symbols with "_". nm -C demangles C++ names, which
    # then contain "(" or "::", and leaves C names as they are.
    name = symbol.name
    if name.startswith("_") and "(" not in name and "::" not in name:
        return Symbol(symbol.address, symbol.size, name[1:])
    return symbol


class NativeNames:
    """Names native frames, loading each library's symbols on first use."""

    def __init__(self, libs: list[dict]) -> None:
        self._libs = libs
        self._loaded: dict[int, LibSymbols | str] = {}

    def __call__(self, lib_index: int, rva: int) -> str | None:
        if lib_index not in self._loaded:
            lib = self._libs[lib_index]
            self._loaded[lib_index] = lib_symbols(Path(lib["path"]), lib["codeId"])
        table = self._loaded[lib_index]
        if isinstance(table, str):
            return None
        symbol = table.lookup(rva)
        return symbol.name if symbol else None

    def unavailable(self) -> dict[str, str]:
        """Why each library native frames were in has no symbols."""
        return {
            self._libs[i]["name"]: reason
            for i, reason in self._loaded.items()
            if isinstance(reason, str)
        }


class Thread:
    def __init__(
        self,
        raw: dict,
        libs: list[dict],
        native_name: Callable[[int, int], str | None] | None,
    ) -> None:
        self.name: str = raw["name"]
        self.tid: int = int(raw["tid"])
        self.pid: int = int(raw["pid"])
        self.is_main: bool = raw.get("isMainThread", False)
        self._samples = raw["samples"]
        if self._samples.get("weightType", "samples") != "samples":
            raise ValueError(
                f"thread {self.name}: unsupported weightType"
                f" {self._samples['weightType']!r}"
            )
        self._stacks = _resolve_stacks(raw, libs, native_name)
        # A sample's CPU delta is the thread's CPU time since the previous
        # sample. For a thread that existed before its first sample (the
        # main thread, from process startup on), the first delta covers that
        # unsampled time and would all land on one arbitrary PC.
        self._cpu = list(self._samples.get("threadCPUDelta") or [])
        times = self._samples["time"]
        if self._cpu and raw.get("registerTime", times[0]) < times[0]:
            self._cpu[0] = 0

    def __len__(self) -> int:
        return self._samples["length"]

    def samples(self) -> Iterator[Sample]:
        s = self._samples
        weights = s.get("weight") or [1] * s["length"]
        cpu = self._cpu or [0] * s["length"]
        for stack, time, weight, delta in zip(
            s["stack"], s["time"], weights, cpu, strict=True
        ):
            frames = () if stack is None else self._stacks[stack]
            yield Sample(frames, time, weight, delta or 0)

    def cpu_us(self) -> float:
        return sum(d or 0 for d in self._cpu)


def _resolve_stacks(
    raw: dict,
    libs: list[dict],
    native_name: Callable[[int, int], str | None] | None,
) -> list[tuple[Frame, ...]]:
    """Leaf-first frames for every stackTable index."""
    strings = raw["stringArray"]
    ft, fn, rt = raw["frameTable"], raw["funcTable"], raw["resourceTable"]

    frames: list[Frame | None] = []
    for i in range(ft["length"]):
        func = ft["func"][i]
        address = ft["address"][i]
        # samply puts a JS-level frame without address above each JIT frame
        # of a JS function. It duplicates the JIT frame's name minus the
        # tier, so drop it.
        if fn["isJS"][func] and address == -1:
            frames.append(None)
            continue
        res = fn["resource"][func]
        lib_index = rt["lib"][res] if res is not None and res >= 0 else None
        lib = libs[lib_index]["name"] if lib_index is not None else None
        name = strings[fn["name"][func]]
        if lib is not None and _is_jitdump(lib):
            frames.append(parse_jit_name(name, lib, address))
        else:
            if lib_index is not None and address is not None:
                symbolized = native_name(lib_index, address) if native_name else None
                name = symbolized or f"{lib}+{address:#x}"
            frames.append(Frame(name, "native", None, lib, address))

    st = raw["stackTable"]
    stacks: list[tuple[Frame, ...]] = []
    for i in range(st["length"]):
        prefix = st["prefix"][i]
        # A prefix always precedes its stack in the table.
        parent = stacks[prefix] if prefix is not None else ()
        frame = frames[st["frame"][i]]
        stacks.append(parent if frame is None else (frame, *parent))
    return stacks


def _is_jitdump(lib_name: str) -> bool:
    return lib_name.startswith("jit-") and lib_name.endswith(".dump")


class Profile:
    def __init__(self, raw: dict, path: Path, *, symbolize: bool = True) -> None:
        self.path = path
        meta = raw["meta"]
        version = meta.get("preprocessedProfileVersion")
        if version not in SUPPORTED_VERSIONS:
            raise ValueError(
                f"{path}: samply profile format version {version} is not"
                f" supported (tested: {sorted(SUPPORTED_VERSIONS)}). Re-record"
                " with a samply release that writes a tested version."
            )
        self.interval_ms: float = meta["interval"]
        self.product: str = meta.get("product", "")
        self.libs: list[dict] = raw["libs"]
        self._jitdumps: dict[str, JitDump] = {}
        natives = NativeNames(self.libs) if symbolize else None
        self.threads = [Thread(t, self.libs, natives) for t in raw["threads"]]
        #: Library name -> why its frames stay "<lib>+<rva>".
        self.unsymbolized: dict[str, str] = natives.unavailable() if natives else {}

    @classmethod
    def load(cls, path: Path | str, *, symbolize: bool = True) -> Profile:
        """Load a profile file, or the profile in a recording directory.
        symbolize=False leaves native frames named "<lib>+<rva>"."""
        path = Path(path).expanduser()
        if path.is_dir():
            path = path / PROFILE_NAME
        with gzip.open(path, "rt") if path.suffix == ".gz" else path.open() as f:
            return cls(json.load(f), path, symbolize=symbolize)

    def main_thread(self) -> Thread:
        """The thread with the most CPU time."""
        return max(self.threads, key=Thread.cpu_us)

    def thread(self, name_or_tid: str | int) -> Thread:
        """A thread by tid, or by name when exactly one thread has it."""
        if isinstance(name_or_tid, int):
            matches = [t for t in self.threads if t.tid == name_or_tid]
        else:
            matches = [t for t in self.threads if t.name == name_or_tid]
        if len(matches) == 1:
            return matches[0]
        problem = "ambiguous" if matches else "no"
        raise ValueError(
            f"{problem} thread {name_or_tid!r}; threads: "
            + ", ".join(f"{t.name} ({t.tid})" for t in self.threads)
        )


# ---------- Analyses ----------
#
# Every analysis weighs samples by CPU time by default. With wall=True it
# weighs them by elapsed time instead: samply folds a blocked stretch into
# one sample with a weight of that many intervals and no CPU time.

_profile_cache: dict[tuple[str, float], Profile] = {}


def load(path: Path | str) -> Profile:
    """Profile.load, cached per file and mtime for repeated tool calls."""
    file = Path(path).expanduser()
    if file.is_dir():
        file = file / PROFILE_NAME
    key = (str(file), file.stat().st_mtime)
    if key not in _profile_cache:
        _profile_cache[key] = Profile.load(file)
    return _profile_cache[key]


def _weight_ms(sample: Sample, interval_ms: float, wall: bool) -> float:
    return sample.weight * interval_ms if wall else sample.cpu_us / 1000


def _select_thread(profile: Profile, thread: str | int | None) -> Thread:
    return profile.main_thread() if thread is None else profile.thread(thread)


@dataclass(frozen=True)
class Function:
    """What analyses aggregate by: a frame without its address. With merged
    tiers, the code flavour fields are cleared too."""

    name: str
    kind: str
    lib: str | None
    tier: str | None = None
    osr: bool = False
    specialized: bool = False

    @classmethod
    def of(cls, frame: Frame, merge_tiers: bool = False) -> Function:
        if merge_tiers:
            return cls(frame.name, frame.kind, frame.lib)
        return cls(
            frame.name, frame.kind, frame.lib, frame.tier, frame.osr, frame.specialized
        )


@dataclass(frozen=True)
class Hotspot:
    function: Function
    self_ms: float
    total_ms: float


@dataclass(frozen=True)
class Hotspots:
    thread: Thread
    wall: bool
    total_ms: float
    rows: list[Hotspot]  # by self time, descending


def hotspots(
    profile: Profile,
    thread: str | int | None = None,
    *,
    n: int = 30,
    wall: bool = False,
    merge_tiers: bool = False,
    by_lib: bool = False,
    filter: str | None = None,
) -> Hotspots:
    """Self and total time per function (or per library with by_lib)."""
    t = _select_thread(profile, thread)
    self_ms: dict[Function, float] = {}
    total_ms: dict[Function, float] = {}
    total = 0.0
    for s in t.samples():
        w = _weight_ms(s, profile.interval_ms, wall)
        total += w
        if not s.frames or not w:
            continue
        if by_lib:
            keys = [Function(f.lib or "(unknown)", "lib", f.lib) for f in s.frames]
        else:
            keys = [Function.of(f, merge_tiers) for f in s.frames]
        self_ms[keys[0]] = self_ms.get(keys[0], 0.0) + w
        for k in set(keys):
            total_ms[k] = total_ms.get(k, 0.0) + w
    rows = [
        Hotspot(k, self_ms.get(k, 0.0), v)
        for k, v in total_ms.items()
        if filter is None or filter in k.name
    ]
    rows.sort(key=lambda r: (-r.self_ms, -r.total_ms, r.function.name))
    return Hotspots(t, wall, total, rows[:n])


def match_functions(profile: Profile, thread: Thread, symbol: str) -> set[str]:
    """Function names in `thread` that `symbol` selects: the exact name when
    one matches, otherwise every name containing it."""
    names = {f.name for s in thread.samples() for f in s.frames}
    if symbol in names:
        return {symbol}
    found = {n for n in names if symbol in n}
    if not found:
        raise ValueError(f"no function matching {symbol!r} in thread {thread.name}")
    return found


@dataclass(frozen=True)
class CallPath:
    callers: tuple[Function, ...]  # nearest caller first
    ms: float


@dataclass(frozen=True)
class Callers:
    thread: Thread
    wall: bool
    total_ms: float  # all samples in the thread
    matched: set[str]
    inclusive_ms: float  # samples with a matched function on the stack
    paths: list[CallPath]  # heaviest first


def callers(
    profile: Profile,
    symbol: str,
    thread: str | int | None = None,
    *,
    depth: int = 5,
    n: int = 20,
    wall: bool = False,
) -> Callers:
    """The call paths leading into functions matching `symbol`, `depth`
    callers deep, weighted by the time spent in (and under) the function.
    The outermost matched frame counts, so recursion is not double-counted."""
    t = _select_thread(profile, thread)
    matched = match_functions(profile, t, symbol)
    paths: dict[tuple[Function, ...], float] = {}
    total = inclusive = 0.0
    for s in t.samples():
        w = _weight_ms(s, profile.interval_ms, wall)
        total += w
        hits = [i for i, f in enumerate(s.frames) if f.name in matched]
        if not hits or not w:
            continue
        inclusive += w
        outer = hits[-1]
        key = tuple(Function.of(f) for f in s.frames[outer + 1 : outer + 1 + depth])
        paths[key] = paths.get(key, 0.0) + w
    ordered = sorted(paths.items(), key=lambda kv: -kv[1])[:n]
    return Callers(
        t, wall, total, matched, inclusive, [CallPath(k, v) for k, v in ordered]
    )


@dataclass(frozen=True)
class StackPath:
    frames: tuple[Function, ...]  # root first
    ms: float


@dataclass(frozen=True)
class Flamegraph:
    thread: Thread
    wall: bool
    total_ms: float
    focus: set[str] | None
    paths: list[StackPath]  # heaviest first


def flamegraph(
    profile: Profile,
    thread: str | int | None = None,
    *,
    focus: str | None = None,
    depth: int = 30,
    min_pct: float = 0.5,
    wall: bool = False,
    native_root: bool = False,
) -> Flamegraph:
    """Collapsed stacks: root-to-leaf paths with their time, cut to `depth`
    frames below the root (or below the focused function, whose subtree is
    then the whole graph). Paths under min_pct of the thread's time are
    dropped.

    Unless native_root, the native frames above a stack's outermost JIT
    frame (process start, the shell's message loop) are left out, so the
    depth goes to the JS side.
    """
    t = _select_thread(profile, thread)
    matched = match_functions(profile, t, focus) if focus else None
    paths: dict[tuple[Function, ...], float] = {}
    total = 0.0
    for s in t.samples():
        w = _weight_ms(s, profile.interval_ms, wall)
        total += w
        if not w:
            continue
        frames = s.frames[::-1]
        if matched is not None:
            start = next((i for i, f in enumerate(frames) if f.name in matched), None)
            if start is None:
                continue
            frames = frames[start:]
        elif not native_root:
            first_jit = next((i for i, f in enumerate(frames) if f.kind != "native"), 0)
            frames = frames[first_jit:]
        key = tuple(Function.of(f) for f in frames[:depth])
        paths[key] = paths.get(key, 0.0) + w
    cutoff = total * min_pct / 100
    kept = sorted(
        (StackPath(k, v) for k, v in paths.items() if v >= cutoff),
        key=lambda p: -p.ms,
    )
    return Flamegraph(t, wall, total, matched, kept)


@dataclass(frozen=True)
class DiffRow:
    function: Function
    base_pct: float | None  # None: absent from the baseline
    after_pct: float | None  # None: absent from the experiment


def diff(
    baseline: Profile,
    experiment: Profile,
    thread: str | int | None = None,
    *,
    n: int = 30,
    wall: bool = False,
    merge_tiers: bool = True,
) -> list[DiffRow]:
    """Per-function self time as a share of each profile's thread time,
    sorted by the size of the change. Tiers are merged by default, so a
    function that tiered up differently still lines up."""

    def shares(profile: Profile) -> dict[Function, float]:
        h = hotspots(profile, thread, n=sys.maxsize, wall=wall, merge_tiers=merge_tiers)
        return {
            r.function: 100 * r.self_ms / h.total_ms
            for r in h.rows
            if r.self_ms and h.total_ms
        }

    a, b = shares(baseline), shares(experiment)
    rows = [DiffRow(k, a.get(k), b.get(k)) for k in a.keys() | b.keys()]
    rows.sort(
        key=lambda r: (-abs((r.after_pct or 0) - (r.base_pct or 0)), r.function.name)
    )
    return rows[:n]


@dataclass(frozen=True)
class ThreadSummary:
    thread: Thread
    wall_ms: float
    cpu_ms: float
    top: list[Hotspot]  # by self CPU time


def threads(profile: Profile, *, top: int = 3) -> list[ThreadSummary]:
    """Every thread's wall and CPU time and its hottest functions, busiest
    first."""
    out = []
    for t in profile.threads:
        wall_ms = sum(s.weight for s in t.samples()) * profile.interval_ms
        h = hotspots(profile, t.tid, n=top)
        out.append(ThreadSummary(t, wall_ms, t.cpu_us() / 1000, h.rows))
    out.sort(key=lambda s: -s.cpu_ms)
    return out


# ---------- Annotate ----------


@dataclass(frozen=True)
class CodeObject:
    """One body of code a function ran in: a JIT code load (a function has
    one per tier and per recompilation) or a native symbol."""

    function: Function
    label: str
    ms: float
    jit: CodeLoad | None = None
    native: tuple[dict, LibSymbols, Symbol] | None = None  # lib, its table, symbol


@dataclass(frozen=True)
class AnnotatedLine:
    address: int | None  # None for a source position line
    text: str
    ms: float


@dataclass(frozen=True)
class Annotation:
    thread: Thread
    wall: bool
    matched: set[str]
    objects: list[CodeObject]  # hottest first
    selected: int  # index into objects
    lines: list[AnnotatedLine]
    unplaced_ms: float  # samples at no instruction boundary of the listing


def _jitdump(profile: Profile, lib_name: str) -> JitDump:
    path = profile.path.parent / lib_name
    key = str(path)
    if key not in profile._jitdumps:
        if not path.is_file():
            raise FileNotFoundError(
                f"{path} not found: annotating JIT code needs the jitdump that"
                " --perf-prof wrote next to the profile"
            )
        profile._jitdumps[key] = JitDump.read(path)
    return profile._jitdumps[key]


def annotate(
    profile: Profile,
    symbol: str,
    thread: str | int | None = None,
    *,
    code: int | None = None,
    wall: bool = False,
    objdump: str | None = None,
) -> Annotation:
    """Per-instruction self time in one code object of the functions
    matching `symbol`: the hottest, or objects[code - 1]."""
    t = _select_thread(profile, thread)
    matched = match_functions(profile, t, symbol)
    by_object: dict[tuple, dict[int, float]] = {}
    objects: dict[tuple, CodeObject] = {}
    for s in t.samples():
        w = _weight_ms(s, profile.interval_ms, wall)
        if not s.frames or not w or s.frames[0].name not in matched:
            continue
        leaf = s.frames[0]
        located = _locate(profile, leaf)
        if located is None:
            continue
        key, pc, obj = located
        objects.setdefault(key, obj)
        pcs = by_object.setdefault(key, {})
        pcs[pc] = pcs.get(pc, 0.0) + w
    if not objects:
        raise ValueError(
            f"no samples with {', '.join(sorted(matched))} as the leaf in a code"
            " object that can be disassembled"
        )
    ranked = sorted(
        (
            CodeObject(o.function, o.label, sum(by_object[k].values()), o.jit, o.native)
            for k, o in objects.items()
        ),
        key=lambda o: -o.ms,
    )
    index = 0 if code is None else code - 1
    if not 0 <= index < len(ranked):
        raise ValueError(f"code must be 1..{len(ranked)}")
    chosen = ranked[index]
    pcs = by_object[_object_key(chosen)]
    objdump = objdump or disasm.find_objdump(config.load().repos["v8"].path)
    lines = (
        _annotate_jit(profile, chosen, pcs, objdump)
        if chosen.jit is not None
        else _annotate_native(chosen, pcs, objdump)
    )
    placed = sum(line.ms for line in lines)
    return Annotation(
        t, wall, matched, ranked, index, lines, sum(pcs.values()) - placed
    )


def _object_key(obj: CodeObject) -> tuple:
    if obj.jit is not None:
        return ("jit", obj.jit.start)
    lib, _, sym = obj.native
    return ("native", lib["name"], sym.address)


def _locate(profile: Profile, leaf: Frame) -> tuple[tuple, int, CodeObject] | None:
    """The code object a leaf frame ran in, and its PC there: a runtime
    address for JIT code, a link-time address for native code."""
    if leaf.lib is None or leaf.address is None:
        return None
    if _is_jitdump(leaf.lib):
        load = _jitdump(profile, leaf.lib).load_at(leaf.address)
        if load is None:
            return None
        obj = CodeObject(Function.of(leaf), load.name, 0.0, jit=load)
        return _object_key(obj), load.code_addr + leaf.address - load.start, obj
    lib = next((lib for lib in profile.libs if lib["name"] == leaf.lib), None)
    if lib is None:
        return None
    table = lib_symbols(Path(lib["path"]), lib["codeId"])
    if isinstance(table, str):
        return None
    sym = table.lookup(leaf.address)
    if sym is None:
        return None
    obj = CodeObject(
        Function.of(leaf), f"{leaf.lib} {sym.name}", 0.0, native=(lib, table, sym)
    )
    return _object_key(obj), leaf.address + table.text_vmaddr, obj


# An address operand in llvm-objdump's listing, with its label if it has one.
# Immediates are written "#0x...", so they don't match.
_ADDRESS_OPERAND = re.compile(r"(?<![#\w])0x([0-9a-f]+)(?: <[^>]*>)?")


def _relabel(text: str, name_at: Callable[[int], str | None]) -> str:
    """Label address operands with name_at(address), or drop the label
    llvm-objdump guessed; the files it sees have no useful symbols."""

    def label(m: re.Match) -> str:
        name = name_at(int(m.group(1), 16))
        return f"0x{m.group(1)} <{name}>" if name else f"0x{m.group(1)}"

    return _ADDRESS_OPERAND.sub(label, text)


def _annotate_jit(
    profile: Profile, obj: CodeObject, pcs: dict[int, float], objdump: str
) -> list[AnnotatedLine]:
    load = obj.jit
    dump = _jitdump(profile, obj.function.lib)
    machine = dump.machine

    def name_at(address: int) -> str | None:
        if load.code_addr <= address < load.code_addr + len(load.code):
            return f"+{address - load.code_addr:#x}"
        target = dump.load_containing(address)
        if target is None:
            return None
        offset = address - target.code_addr
        return f"{target.name}+{offset:#x}" if offset else target.name

    source = _SourceReader(profile.path.parent)
    lines: list[AnnotatedLine] = []
    last = None
    for insn in disasm.disassemble_code(load.code, load.code_addr, machine, objdump):
        position = load.position_at(insn.address - load.code_addr)
        if position is not None and (position.script, position.line) != last:
            last = (position.script, position.line)
            text = source.line(position.script, position.line)
            where = f"{position.script}:{position.line}:{position.column}"
            lines.append(
                AnnotatedLine(None, f"{where}  {text}" if text else where, 0.0)
            )
        text = _relabel(insn.text, name_at)
        lines.append(AnnotatedLine(insn.address, text, pcs.get(insn.address, 0.0)))
    return lines


def _annotate_native(
    obj: CodeObject, pcs: dict[int, float], objdump: str
) -> list[AnnotatedLine]:
    lib, table, sym = obj.native
    path = Path(lib["path"])
    image = next(i for i in macho.images(path) if i.uuid == lib["codeId"])
    with macho.thin_file(path, image) as thin:
        insns = disasm.disassemble_range(
            thin, sym.address, sym.address + sym.size, objdump
        )

    def name_at(address: int) -> str | None:
        target = table.lookup(address - table.text_vmaddr)
        if target is None:
            return None
        offset = address - target.address
        if target == sym:
            return f"+{offset:#x}"
        return f"{target.name}+{offset:#x}" if offset else target.name

    return [
        AnnotatedLine(i.address, _relabel(i.text, name_at), pcs.get(i.address, 0.0))
        for i in insns
    ]


class _SourceReader:
    """Script lines for source positions. Scripts are named as V8 saw them:
    absolute, or relative to the directory d8 ran in, which a jsb
    recording's meta.json names."""

    def __init__(self, rec_dir: Path) -> None:
        self._base: Path | None = None
        meta = rec_dir / "meta.json"
        if meta.is_file():
            self._base = Path(json.loads(meta.read_text())["suite_dir"])
        self._files: dict[str, list[str] | None] = {}

    def line(self, script: str, line: int) -> str | None:
        if script not in self._files:
            self._files[script] = self._read(script)
        lines = self._files[script]
        if lines is None or not 0 < line <= len(lines):
            return None
        return lines[line - 1].strip()

    def _read(self, script: str) -> list[str] | None:
        path = Path(script)
        if not path.is_absolute():
            if self._base is None:
                return None
            path = self._base / path
        try:
            return path.read_text(errors="replace").splitlines()
        except OSError:
            return None
