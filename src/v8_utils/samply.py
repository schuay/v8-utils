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
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from . import macho
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
        self._text_vmaddr = text_vmaddr

    def lookup(self, rva: int) -> Symbol | None:
        address = rva + self._text_vmaddr
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
    elif image.universal:
        # nm selects slices by architecture name, which not every slice has
        # (arm64e.x1), so give it the slice as a file of its own.
        with path.open("rb") as f, tempfile.NamedTemporaryFile() as thin:
            f.seek(image.offset)
            thin.write(f.read(image.size))
            thin.flush()
            symbols = read_symbols(Path(thin.name), text_only=True)
    else:
        symbols = read_symbols(path, text_only=True)
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

    def __len__(self) -> int:
        return self._samples["length"]

    def samples(self) -> Iterator[Sample]:
        s = self._samples
        weights = s.get("weight") or [1] * s["length"]
        cpu = s.get("threadCPUDelta") or [0] * s["length"]
        for stack, time, weight, delta in zip(
            s["stack"], s["time"], weights, cpu, strict=True
        ):
            frames = () if stack is None else self._stacks[stack]
            yield Sample(frames, time, weight, delta or 0)

    def cpu_us(self) -> float:
        return sum(d or 0 for d in self._samples.get("threadCPUDelta") or ())


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
