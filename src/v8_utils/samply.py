"""samply profiles: loading, stack resolution and frame classification.

`samply record --save-only` writes a Firefox processed profile (gzipped
JSON). Frame addresses are relative to their library: an RVA for native
code, and for V8 JIT code an offset into the jitdump, which samply presents
as one library made of every JIT_CODE_LOAD record's code bytes, concatenated
in file order. JIT frames arrive named from the jitdump; native frames are
unsymbolicated.

Leaf addresses are exact PCs; caller addresses are return address - 1.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

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


class Thread:
    def __init__(self, raw: dict, libs: list[dict]) -> None:
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
        self._stacks = _resolve_stacks(raw, libs)

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


def _resolve_stacks(raw: dict, libs: list[dict]) -> list[tuple[Frame, ...]]:
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
            if lib is not None and address is not None:
                name = f"{lib}+{address:#x}"
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
    def __init__(self, raw: dict, path: Path) -> None:
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
        self.threads = [Thread(t, self.libs) for t in raw["threads"]]

    @classmethod
    def load(cls, path: Path | str) -> Profile:
        """Load a profile file, or the profile in a recording directory."""
        path = Path(path).expanduser()
        if path.is_dir():
            path = path / PROFILE_NAME
        with gzip.open(path, "rt") if path.suffix == ".gz" else path.open() as f:
            return cls(json.load(f), path)

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
