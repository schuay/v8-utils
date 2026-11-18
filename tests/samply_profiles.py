"""Synthetic samply profiles for tests, in the processed-profile layout
samply 0.13 writes."""

JIT = "jit-1.dump"


def raw_profile(*threads, libs: list[dict] | None = None) -> dict:
    """threads: (name, tid, samples); a sample is (root-first frames, weight,
    cpu_us). A frame is a jitdump name ("JS:*f a.js:1:1"), placed in libs[0]
    at a made-up address, or (name, lib index, address). libs defaults to
    one jitdump.

    Each thread starts with a stackless, weightless sample after its
    registration, standing in for samply's first sample of a thread that ran
    before sampling started, whose CPU delta is not counted.
    """
    libs = libs or [{"name": JIT, "path": f"/rec/{JIT}", "codeId": ""}]
    raw_threads = []
    for name, tid, samples in threads:
        samples = [((), 0, 5000), *samples]
        strings: list[str] = []
        funcs: dict[tuple[str, int], int] = {}
        frames: dict[tuple[str, int, int], int] = {}
        ft = {"address": [], "func": []}
        fn = {"name": [], "resource": []}
        stacks: dict[tuple[int, ...], int] = {}
        st = {"prefix": [], "frame": []}
        sample_stacks = []
        for stack, _, _ in samples:
            prefix = None
            path: tuple[int, ...] = ()
            for spec in stack:
                fname, lib, address = (spec, 0, None) if isinstance(spec, str) else spec
                if (fname, lib) not in funcs:
                    funcs[(fname, lib)] = len(fn["name"])
                    fn["name"].append(len(strings))
                    strings.append(fname)
                    fn["resource"].append(lib)
                func = funcs[(fname, lib)]
                if address is None:
                    address = 0x100 * func
                if (fname, lib, address) not in frames:
                    frames[(fname, lib, address)] = len(ft["func"])
                    ft["address"].append(address)
                    ft["func"].append(func)
                path += (frames[(fname, lib, address)],)
                if path not in stacks:
                    stacks[path] = len(st["prefix"])
                    st["prefix"].append(prefix)
                    st["frame"].append(path[-1])
                prefix = stacks[path]
            sample_stacks.append(prefix)
        raw_threads.append(
            {
                "name": name,
                "tid": str(tid),
                "pid": "1",
                "isMainThread": tid == 1,
                "registerTime": -1.0,
                "stringArray": strings,
                "frameTable": {"length": len(ft["func"]), **ft},
                "funcTable": {
                    "length": len(fn["name"]),
                    "isJS": [False] * len(fn["name"]),
                    **fn,
                },
                "resourceTable": {"length": len(libs), "lib": list(range(len(libs)))},
                "stackTable": {"length": len(st["prefix"]), **st},
                "samples": {
                    "length": len(samples),
                    "weightType": "samples",
                    "stack": sample_stacks,
                    "time": [float(i) for i in range(len(samples))],
                    "weight": [w for _, w, _ in samples],
                    "threadCPUDelta": [c for _, _, c in samples],
                },
            }
        )
    return {
        "meta": {"preprocessedProfileVersion": 49, "interval": 1.0},
        "libs": libs,
        "threads": raw_threads,
    }
