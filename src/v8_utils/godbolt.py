"""Compiling snippets on Compiler Explorer (godbolt.org)."""

_godbolt_compiler_cache: dict[str, list[dict]] | None = None

_GODBOLT_ISET_MAP = {
    "x64": {"amd64", "x86-64", "x86_64"},
    "arm64": {"aarch64", "arm64"},
}

# Default compiler IDs per arch — Godbolt-maintained trunk builds.
_GODBOLT_DEFAULT_COMPILER = {
    "x64": "clang_trunk",
    "arm64": "armv8-clang-trunk",
}

_MCA_DEFAULT_CPU = {"x64": "skylake", "arm64": "cortex-a76"}


def _godbolt_get_compilers(language: str) -> list[dict]:
    """Fetch and cache compiler list from Godbolt. Cached per-language for process lifetime."""
    import httpx

    global _godbolt_compiler_cache
    if _godbolt_compiler_cache is None:
        _godbolt_compiler_cache = {}
    if language not in _godbolt_compiler_cache:
        r = httpx.get(
            f"https://godbolt.org/api/compilers/{language}",
            params={"fields": "id,name,semver,instructionSet"},
            headers={"Accept": "application/json"},
            timeout=30,
        )
        r.raise_for_status()
        _godbolt_compiler_cache[language] = r.json()
    return _godbolt_compiler_cache[language]


def _godbolt_infer_arch(compiler_id: str, language: str) -> str:
    """Infer arch from a Godbolt compiler's instruction set metadata."""
    for c in _godbolt_get_compilers(language):
        if c.get("id") == compiler_id:
            iset = (c.get("instructionSet") or "").lower()
            for arch, aliases in _GODBOLT_ISET_MAP.items():
                if iset in aliases:
                    return arch
            break
    return "x64"


def godbolt_compile(
    source: str,
    arch: str = "x64",
    compiler: str | None = None,
    language: str = "c++",
    flags: str = "-O3 -fno-strict-aliasing -fno-omit-frame-pointer",
    mca: bool = True,
    opt_remarks: bool = False,
) -> str:
    """Compile a code snippet on Godbolt and return the assembly output.

    By default uses the latest clang trunk and runs llvm-mca analysis.
    """
    import httpx

    compiler_id = compiler or _GODBOLT_DEFAULT_COMPILER.get(arch)
    if compiler_id is None:
        return (
            f"Unknown arch {arch!r}. Use 'x64' or 'arm64', "
            f"or pass an explicit compiler ID."
        )

    # When compiler is explicitly specified, infer arch from metadata for MCA.
    if compiler is not None:
        arch = _godbolt_infer_arch(compiler_id, language)

    if (mca or opt_remarks) and "clang" not in compiler_id.lower():
        return "Error: mca and opt_remarks require a Clang compiler."

    options: dict = {
        "userArguments": flags,
        "filters": {
            "intel": True,
            "demangle": True,
            "commentOnly": True,
            "directives": True,
        },
    }

    if mca:
        cpu = _MCA_DEFAULT_CPU.get(arch, "")
        mca_arg = f"-mcpu={cpu}" if cpu else ""
        options["tools"] = [{"id": "llvm-mcatrunk", "args": mca_arg}]

    if opt_remarks:
        options["compilerOptions"] = {"produceOptInfo": True}

    r = httpx.post(
        f"https://godbolt.org/api/compiler/{compiler_id}/compile",
        json={"source": source, "lang": language, "options": options},
        headers={"Accept": "application/json"},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()

    lines: list[str] = [f"# {compiler_id} {flags}"]

    stderr_lines = data.get("stderr") or []
    if stderr_lines:
        for s in stderr_lines:
            lines.append(s.get("text", ""))
        lines.append("")

    asm_lines = data.get("asm") or []
    for a in asm_lines:
        lines.append(a.get("text", ""))

    if mca:
        for tool_entry in data.get("tools") or []:
            if tool_entry.get("id") == "llvm-mcatrunk":
                lines.append("")
                lines.append("# --- llvm-mca analysis ---")
                for s in tool_entry.get("stderr") or []:
                    lines.append(s.get("text", ""))
                for s in tool_entry.get("stdout") or []:
                    lines.append(s.get("text", ""))

    if opt_remarks:
        opt_output = data.get("optOutput") or []
        if opt_output:
            lines.append("")
            lines.append("# --- optimization remarks ---")
            for opt_type in ("Missed", "Passed", "Analysis"):
                entries = [o for o in opt_output if o.get("optType") == opt_type]
                if not entries:
                    continue
                lines.append(f"# {opt_type} ({len(entries)}):")
                for o in entries:
                    loc = o.get("DebugLoc") or {}
                    loc_str = (
                        f"{loc.get('File', '?')}:{loc.get('Line', '?')}" if loc else ""
                    )
                    fn = o.get("Function", "")
                    display = o.get("displayString", "")
                    lines.append(f"  [{fn}] {loc_str}: {display}")

    return "\n".join(lines)


def godbolt_list_compilers(
    language: str = "c++",
    filter: str | None = None,
) -> str:
    """List available compilers on Godbolt for a language. Use filter to narrow results."""
    compilers = _godbolt_get_compilers(language)

    if filter:
        needle = filter.lower()
        compilers = [
            c
            for c in compilers
            if needle in (c.get("id") or "").lower()
            or needle in (c.get("name") or "").lower()
            or needle in (c.get("instructionSet") or "").lower()
        ]

    lines = [f"{'id':<30} {'name':<45} {'instructionSet'}"]
    lines.append("-" * len(lines[0]))
    for c in compilers:
        lines.append(
            f"{c.get('id', ''):<30} {c.get('name', ''):<45} {c.get('instructionSet', '')}"
        )

    if len(lines) == 2:
        return "No compilers matched the filter."

    return "\n".join(lines)
