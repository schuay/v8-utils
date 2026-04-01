"""Unit tests for v8log — parser, analysis, and formatting."""

from textwrap import dedent

import pytest

from v8_utils.v8log import (
    CodeEntry,
    CodeMap,
    CppSymbolizer,
    SharedLibrary,
    V8Log,
    _split_line,
    _unescape,
    analyze_deopts,
    analyze_fn,
    analyze_ics,
    analyze_maps,
    analyze_profile,
    analyze_vms,
    format_deopts,
    format_fn,
    format_ics,
    format_maps,
    format_profile,
    format_vms,
)


# ══════════════════════════════════════════════════════════════════════════════
# CSV parsing
# ══════════════════════════════════════════════════════════════════════════════


class TestUnescape:
    def test_no_escapes(self):
        assert _unescape("hello") == "hello"

    def test_comma_escape(self):
        assert _unescape("foo\\x2Cbar") == "foo,bar"

    def test_hex_escape(self):
        assert _unescape("\\x41") == "A"

    def test_unicode_escape(self):
        assert _unescape("\\u0041") == "A"

    def test_newline_escape(self):
        assert _unescape("line1\\nline2") == "line1\nline2"

    def test_backslash_escape(self):
        assert _unescape("a\\\\b") == "a\\b"

    def test_multiple_escapes(self):
        assert _unescape("a\\x2Cb\\nc") == "a,b\nc"

    def test_empty_string(self):
        assert _unescape("") == ""


class TestSplitLine:
    def test_simple(self):
        assert _split_line("a,b,c") == ["a", "b", "c"]

    def test_with_escapes(self):
        assert _split_line("a\\x2Cb,c") == ["a,b", "c"]

    def test_empty_fields(self):
        assert _split_line("a,,c") == ["a", "", "c"]

    def test_single_field(self):
        assert _split_line("abc") == ["abc"]


# ══════════════════════════════════════════════════════════════════════════════
# CodeEntry
# ══════════════════════════════════════════════════════════════════════════════


class TestCodeEntry:
    def test_tier_ignition(self):
        e = CodeEntry("LazyCompile", "foo test.js:1:1", 0x1000, 100, 0, "~")
        assert e.tier == "ignition"
        assert e.tier_marker == "~"

    def test_tier_sparkplug(self):
        e = CodeEntry("LazyCompile", "foo test.js:1:1", 0x1000, 100, 0, "^")
        assert e.tier == "sparkplug"
        assert e.tier_marker == "^"

    def test_tier_maglev(self):
        e = CodeEntry("LazyCompile", "foo test.js:1:1", 0x1000, 100, 0, "+")
        assert e.tier == "maglev"

    def test_tier_maglev_osr(self):
        e = CodeEntry("LazyCompile", "foo test.js:1:1", 0x1000, 100, 0, "o+")
        assert e.tier == "maglev"

    def test_tier_turbofan(self):
        e = CodeEntry("LazyCompile", "foo test.js:1:1", 0x1000, 100, 0, "*")
        assert e.tier == "turbofan"
        assert e.tier_marker == "*"

    def test_tier_turbofan_osr(self):
        e = CodeEntry("LazyCompile", "foo test.js:1:1", 0x1000, 100, 0, "o*")
        assert e.tier == "turbofan"

    def test_tier_builtin(self):
        e = CodeEntry("Builtin", "ArrayPush", 0x1000, 100, 0, "")
        assert e.tier == "compiled"
        assert e.tier_marker == ""

    def test_func_name(self):
        e = CodeEntry("LazyCompile", "myFunc test.js:10:5", 0x1000, 100, 0, "~")
        assert e.func_name == "myFunc"

    def test_source(self):
        e = CodeEntry("LazyCompile", "myFunc test.js:10:5", 0x1000, 100, 0, "~")
        assert e.source == "test.js:10:5"

    def test_builtin_no_source(self):
        e = CodeEntry("Builtin", "ArrayPush", 0x1000, 100, 0, "")
        assert e.func_name == "ArrayPush"
        assert e.source == ""


# ══════════════════════════════════════════════════════════════════════════════
# CodeMap
# ══════════════════════════════════════════════════════════════════════════════


class TestCodeMap:
    def _entry(self, start, size=100, name="fn"):
        return CodeEntry("LazyCompile", name, start, size, 0, "~")

    def test_add_and_lookup(self):
        cm = CodeMap()
        e = self._entry(0x1000)
        cm.add(e)
        assert cm.lookup(0x1000) is e
        assert cm.lookup(0x1050) is e
        assert cm.lookup(0x1063) is e

    def test_lookup_outside_range(self):
        cm = CodeMap()
        cm.add(self._entry(0x1000, size=100))
        assert cm.lookup(0x0FFF) is None
        assert cm.lookup(0x1064) is None

    def test_lookup_empty(self):
        cm = CodeMap()
        assert cm.lookup(0x1000) is None

    def test_multiple_entries(self):
        cm = CodeMap()
        e1 = self._entry(0x1000, name="first")
        e2 = self._entry(0x2000, name="second")
        cm.add(e1)
        cm.add(e2)
        assert cm.lookup(0x1050) is e1
        assert cm.lookup(0x2050) is e2

    def test_move(self):
        cm = CodeMap()
        e = self._entry(0x1000)
        cm.add(e)
        cm.move(0x1000, 0x3000)
        assert cm.lookup(0x1000) is None
        assert cm.lookup(0x3050) is e
        assert e.start == 0x3000

    def test_delete(self):
        cm = CodeMap()
        e = self._entry(0x1000)
        cm.add(e)
        cm.delete(0x1000)
        assert cm.lookup(0x1000) is None

    def test_move_nonexistent(self):
        cm = CodeMap()
        cm.move(0x1000, 0x2000)  # should not raise

    def test_delete_nonexistent(self):
        cm = CodeMap()
        cm.delete(0x1000)  # should not raise

    def test_all_entries(self):
        cm = CodeMap()
        e1 = self._entry(0x1000)
        e2 = self._entry(0x2000)
        cm.add(e1)
        cm.add(e2)
        assert len(cm.all_entries()) == 2


# ══════════════════════════════════════════════════════════════════════════════
# V8Log.parse
# ══════════════════════════════════════════════════════════════════════════════


# Minimal valid v8.log content for testing
_MINIMAL_LOG = dedent("""\
    v8-version,14,8,0,0,1
    shared-library,/usr/lib/libc.so.6,0x7f000000,0x7f100000,0
    code-creation,Builtin,3,100,0x1000,200,ArrayPush
    code-creation,LazyCompile,0,200,0x2000,300,hot test.js:10:5,0x5000,~
    code-creation,LazyCompile,0,300,0x3000,300,hot test.js:10:5,0x5000,*
    tick,0x2050,1000,0,0x0,0
    tick,0x2050,2000,0,0x0,0
    tick,0x2050,3000,0,0x0,0
    tick,0x1050,4000,0,0x0,1
    tick,0x1050,5000,0,0x0,0
""")


@pytest.fixture()
def minimal_log(tmp_path):
    p = tmp_path / "v8.log"
    p.write_text(_MINIMAL_LOG)
    return p


@pytest.fixture()
def parsed_log(minimal_log):
    return V8Log.parse(minimal_log)


class TestV8LogParse:
    def test_shared_library(self, parsed_log):
        assert len(parsed_log.shared_libs) == 1
        lib = parsed_log.shared_libs[0]
        assert lib.name == "/usr/lib/libc.so.6"
        assert lib.start == 0x7F000000
        assert lib.end == 0x7F100000

    def test_code_entries(self, parsed_log):
        entries = parsed_log.code_map.all_entries()
        assert len(entries) == 3
        builtin = parsed_log.code_map.lookup(0x1050)
        assert builtin is not None
        assert builtin.type == "Builtin"
        assert builtin.func_name == "ArrayPush"

    def test_code_entry_tier(self, parsed_log):
        # The ignition entry was at 0x2000, but the turbofan entry
        # was also added at 0x3000
        entries = parsed_log.code_map.all_entries()
        tiers = {e.tier for e in entries if e.func_name == "hot"}
        assert "ignition" in tiers
        assert "turbofan" in tiers

    def test_ticks(self, parsed_log):
        assert len(parsed_log.ticks) == 5

    def test_tick_vm_state(self, parsed_log):
        states = [t.vm_state for t in parsed_log.ticks]
        assert states.count(0) == 4  # JS
        assert states.count(1) == 1  # GC

    def test_progress_callback(self, minimal_log):
        calls = []
        V8Log.parse(minimal_log, on_progress=lambda d, t: calls.append((d, t)))
        # Progress should report done == total at the end
        assert calls[-1][0] == calls[-1][1]


class TestV8LogParseCodeMove:
    def test_code_move(self, tmp_path):
        log_text = dedent("""\
            code-creation,LazyCompile,0,100,0x1000,200,fn test.js:1:1,0x5000,~
            code-move,0x1000,0x9000
            tick,0x9050,1000,0,0x0,0
        """)
        p = tmp_path / "v8.log"
        p.write_text(log_text)
        log = V8Log.parse(p)
        # After move, old address should not resolve
        assert log.code_map.lookup(0x1000) is None
        # New address should
        entry = log.code_map.lookup(0x9050)
        assert entry is not None
        assert entry.func_name == "fn"


class TestV8LogParseDeopt:
    def test_deopt_parsing(self, tmp_path):
        log_text = dedent("""\
            code-creation,LazyCompile,0,100,0x1000,200,fn test.js:1:1,0x5000,*
            code-deopt,500,200,0x1000,0,10,eager,<test.js:2:5>,wrong map
        """)
        p = tmp_path / "v8.log"
        p.write_text(log_text)
        log = V8Log.parse(p)
        assert len(log.deopts) == 1
        d = log.deopts[0]
        assert d.deopt_kind == "eager"
        assert d.deopt_reason == "wrong map"
        assert d.instruction_start == 0x1000


class TestV8LogParseIc:
    def test_ic_parsing(self, tmp_path):
        log_text = dedent("""\
            code-creation,LazyCompile,0,100,0x1000,200,fn test.js:1:1,0x5000,~
            LoadIC,0x1050,200,10,5,0,1,0xabc,x,,
        """)
        p = tmp_path / "v8.log"
        p.write_text(log_text)
        log = V8Log.parse(p)
        assert len(log.ics) == 1
        ic = log.ics[0]
        assert ic.ic_type == "LoadIC"
        assert ic.line == 10
        assert ic.column == 5
        assert ic.old_state == "0"
        assert ic.new_state == "1"
        assert ic.key == "x"


class TestV8LogParseMap:
    def test_map_create(self, tmp_path):
        log_text = "map-create,100,0xabc123\n"
        p = tmp_path / "v8.log"
        p.write_text(log_text)
        log = V8Log.parse(p)
        assert len(log.maps) == 1
        assert log.maps[0].event_type == "create"
        assert log.maps[0].map_id == "0xabc123"

    def test_map_transition(self, tmp_path):
        log_text = "map,Transition,200,0xabc,0xdef,0x1000,10,5,CopyAddDescriptor,x\n"
        p = tmp_path / "v8.log"
        p.write_text(log_text)
        log = V8Log.parse(p)
        assert len(log.maps) == 1
        m = log.maps[0]
        assert m.event_type == "transition"
        assert m.from_id == "0xabc"
        assert m.to_id == "0xdef"
        assert m.reason == "CopyAddDescriptor"
        assert m.name == "x"

    def test_map_details(self, tmp_path):
        log_text = "map-details,300,0xabc,some details\\nwith newlines\n"
        p = tmp_path / "v8.log"
        p.write_text(log_text)
        log = V8Log.parse(p)
        assert len(log.maps) == 1
        m = log.maps[0]
        assert m.event_type == "details"
        assert "some details\nwith newlines" in m.details


class TestV8LogParseTickStack:
    def test_absolute_stack(self, tmp_path):
        log_text = dedent("""\
            code-creation,LazyCompile,0,100,0x1000,200,caller test.js:1:1,0x5000,~
            code-creation,LazyCompile,0,100,0x2000,200,callee test.js:5:1,0x6000,~
            tick,0x2050,1000,0,0x0,0,0x1050
        """)
        p = tmp_path / "v8.log"
        p.write_text(log_text)
        log = V8Log.parse(p)
        assert len(log.ticks) == 1
        tick = log.ticks[0]
        # Stack: pc=0x2050, then absolute frame 0x1050
        assert tick.stack == [0x2050, 0x1050]

    def test_relative_stack_positive(self, tmp_path):
        log_text = dedent("""\
            code-creation,LazyCompile,0,100,0x1000,0x500,fn test.js:1:1,0x5000,~
            tick,0x1000,1000,0,0x0,0,+0x100
        """)
        p = tmp_path / "v8.log"
        p.write_text(log_text)
        log = V8Log.parse(p)
        tick = log.ticks[0]
        # Stack: pc=0x1000, then pc + 0x100 = 0x1100
        assert tick.stack == [0x1000, 0x1100]

    def test_relative_stack_negative(self, tmp_path):
        log_text = dedent("""\
            code-creation,LazyCompile,0,100,0x2000,0x500,fn test.js:1:1,0x5000,~
            tick,0x2000,1000,0,0x0,0,-0x100
        """)
        p = tmp_path / "v8.log"
        p.write_text(log_text)
        log = V8Log.parse(p)
        tick = log.ticks[0]
        assert tick.stack == [0x2000, 0x1F00]

    def test_overflow_skipped(self, tmp_path):
        log_text = "tick,0x1000,1000,0,0x0,0,overflow\n"
        p = tmp_path / "v8.log"
        p.write_text(log_text)
        log = V8Log.parse(p)
        tick = log.ticks[0]
        assert tick.stack == [0x1000]  # overflow frame ignored

    def test_tos_js_prepended(self, tmp_path):
        log_text = dedent("""\
            code-creation,LazyCompile,0,100,0x1000,0x200,fn test.js:1:1,0x5000,~
            tick,0x1000,1000,0,0x1050,0
        """)
        p = tmp_path / "v8.log"
        p.write_text(log_text)
        log = V8Log.parse(p)
        tick = log.ticks[0]
        # tos 0x1050 resolves to JS code → prepended
        assert tick.stack == [0x1000, 0x1050]

    def test_tos_non_js_not_prepended(self, tmp_path):
        log_text = dedent("""\
            code-creation,Builtin,3,100,0x1000,0x200,SomeBuiltin
            tick,0x1000,1000,0,0x1050,0
        """)
        p = tmp_path / "v8.log"
        p.write_text(log_text)
        log = V8Log.parse(p)
        tick = log.ticks[0]
        # tos resolves to Builtin, not JS → not prepended
        assert tick.stack == [0x1000]


class TestV8LogParseMalformed:
    def test_empty_lines_skipped(self, tmp_path):
        log_text = "\n\ncode-creation,Builtin,3,100,0x1000,200,Foo\n\n"
        p = tmp_path / "v8.log"
        p.write_text(log_text)
        log = V8Log.parse(p)
        assert len(log.code_map.all_entries()) == 1

    def test_unknown_events_skipped(self, tmp_path):
        log_text = "unknown-event,1,2,3\ncode-creation,Builtin,3,100,0x1000,200,Foo\n"
        p = tmp_path / "v8.log"
        p.write_text(log_text)
        log = V8Log.parse(p)
        assert len(log.code_map.all_entries()) == 1

    def test_truncated_line_skipped(self, tmp_path):
        log_text = "code-creation,Builtin\ncode-creation,Builtin,3,100,0x1000,200,Foo\n"
        p = tmp_path / "v8.log"
        p.write_text(log_text)
        log = V8Log.parse(p)
        assert len(log.code_map.all_entries()) == 1


class TestV8LogParseScriptSource:
    def test_script_source(self, tmp_path):
        log_text = "script-source,42,test.js,function foo() {}\n"
        p = tmp_path / "v8.log"
        p.write_text(log_text)
        log = V8Log.parse(p)
        assert 42 in log.scripts
        assert log.scripts[42] == ("test.js", "function foo() {}")


# ══════════════════════════════════════════════════════════════════════════════
# Analysis: deopts
# ══════════════════════════════════════════════════════════════════════════════


@pytest.fixture()
def deopt_log(tmp_path):
    log_text = dedent("""\
        code-creation,LazyCompile,0,100,0x1000,200,hot test.js:10:5,0x5000,*
        code-creation,LazyCompile,0,100,0x2000,200,cold test.js:20:1,0x6000,*
        code-deopt,500,200,0x1000,0,10,eager,<test.js:11:3>,wrong map
        code-deopt,600,200,0x1000,0,10,eager,<test.js:11:3>,wrong map
        code-deopt,700,200,0x1000,0,10,lazy,<test.js:12:5>,not a Smi
        code-deopt,800,200,0x2000,0,5,soft,<test.js:21:1>,insufficient feedback
    """)
    p = tmp_path / "v8.log"
    p.write_text(log_text)
    return V8Log.parse(p)


class TestAnalyzeDeopts:
    def test_total(self, deopt_log):
        s = analyze_deopts(deopt_log)
        assert s.total == 4

    def test_by_kind(self, deopt_log):
        s = analyze_deopts(deopt_log)
        assert s.by_kind["eager"] == 2
        assert s.by_kind["lazy"] == 1
        assert s.by_kind["soft"] == 1

    def test_by_reason(self, deopt_log):
        s = analyze_deopts(deopt_log)
        reasons = dict(s.by_reason)
        assert reasons["wrong map"] == 2
        assert reasons["not a Smi"] == 1

    def test_hot_sites(self, deopt_log):
        s = analyze_deopts(deopt_log)
        top = s.hot_sites[0]
        assert top[0] == "hot"  # func name
        assert top[3] == 2  # count

    def test_filter(self, deopt_log):
        s = analyze_deopts(deopt_log, filter_pat="cold")
        assert s.total == 1
        assert s.hot_sites[0][0] == "cold"

    def test_filter_no_match(self, deopt_log):
        s = analyze_deopts(deopt_log, filter_pat="nonexistent")
        assert s.total == 0

    def test_format_not_empty(self, deopt_log):
        s = analyze_deopts(deopt_log)
        text = format_deopts(s)
        assert "Deopts: 4 total" in text
        assert "wrong map" in text
        assert "Hot sites" in text


# ══════════════════════════════════════════════════════════════════════════════
# Analysis: ICs
# ══════════════════════════════════════════════════════════════════════════════


@pytest.fixture()
def ic_log(tmp_path):
    log_text = dedent("""\
        code-creation,LazyCompile,0,100,0x1000,200,fn test.js:1:1,0x5000,~
        LoadIC,0x1050,200,10,5,0,1,0xabc,x,,
        LoadIC,0x1050,300,10,5,1,N,0xdef,x,,
        StoreIC,0x1080,400,15,3,0,1,0xabc,y,,
        LoadIC,0x1050,500,10,5,N,N,0xghi,x,,
    """)
    p = tmp_path / "v8.log"
    p.write_text(log_text)
    return V8Log.parse(p)


class TestAnalyzeIcs:
    def test_total(self, ic_log):
        s = analyze_ics(ic_log)
        assert s.total == 4

    def test_by_state(self, ic_log):
        s = analyze_ics(ic_log)
        assert s.by_state["monomorphic"] == 2  # transitions to 1
        assert s.by_state["megamorphic"] == 2  # transitions to N

    def test_hot_mega(self, ic_log):
        s = analyze_ics(ic_log)
        assert len(s.hot_mega) >= 1
        top = s.hot_mega[0]
        assert top[0] == "fn"  # function name
        assert top[2] == "LoadIC"  # ic type
        assert top[3] == "x"  # key

    def test_filter(self, ic_log):
        s = analyze_ics(ic_log, filter_pat="nonexistent")
        assert s.total == 0

    def test_format_not_empty(self, ic_log):
        s = analyze_ics(ic_log)
        text = format_ics(s)
        assert "ICs:" in text
        assert "megamorphic" in text


# ══════════════════════════════════════════════════════════════════════════════
# Analysis: maps
# ══════════════════════════════════════════════════════════════════════════════


@pytest.fixture()
def map_log(tmp_path):
    log_text = dedent("""\
        map-create,100,0xaaa
        map-create,110,0xbbb
        map,Transition,200,0xaaa,0xbbb,0x1000,10,5,CopyAddDescriptor,x
        map,Transition,300,0xbbb,0xccc,0x1000,11,5,CopyAddDescriptor,y
        map,Transition,400,0xaaa,0xddd,0x1000,12,5,CopyAddDescriptor,z
        map-details,500,0xccc,deprecated map\\nsome details
    """)
    p = tmp_path / "v8.log"
    p.write_text(log_text)
    return V8Log.parse(p)


class TestAnalyzeMaps:
    def test_creates(self, map_log):
        s = analyze_maps(map_log)
        assert s.creates == 2

    def test_transitions(self, map_log):
        s = analyze_maps(map_log)
        assert s.transitions == 3

    def test_by_reason(self, map_log):
        s = analyze_maps(map_log)
        reasons = dict(s.by_reason)
        assert reasons["CopyAddDescriptor"] == 3

    def test_deprecated(self, map_log):
        s = analyze_maps(map_log)
        assert len(s.deprecated) == 1
        assert s.deprecated[0][0] == "0xccc"

    def test_details_map(self, map_log):
        s = analyze_maps(map_log)
        assert "0xccc" in s.details_map
        assert "deprecated" in s.details_map["0xccc"]

    def test_format_not_empty(self, map_log):
        s = analyze_maps(map_log)
        text = format_maps(s)
        assert "Maps created: 2" in text
        assert "Transitions: 3" in text

    def test_format_verbose(self, map_log):
        s = analyze_maps(map_log)
        text = format_maps(s, verbose=True)
        assert "Map details:" in text
        assert "some details" in text


# ══════════════════════════════════════════════════════════════════════════════
# Analysis: profile
# ══════════════════════════════════════════════════════════════════════════════


class TestAnalyzeProfile:
    def test_total_ticks(self, parsed_log):
        s = analyze_profile(parsed_log)
        assert s.total_ticks == 5

    def test_vm_state_counts(self, parsed_log):
        s = analyze_profile(parsed_log)
        assert s.vm_state_counts["JS"] == 4
        assert s.vm_state_counts["GC"] == 1

    def test_entries_sorted_by_ticks(self, parsed_log):
        s = analyze_profile(parsed_log)
        ticks = [e.self_ticks for e in s.entries]
        assert ticks == sorted(ticks, reverse=True)

    def test_top_limits(self, parsed_log):
        s = analyze_profile(parsed_log, top=1)
        assert len(s.entries) == 1

    def test_format_not_empty(self, parsed_log):
        text = format_profile(analyze_profile(parsed_log))
        assert "VM state:" in text
        assert "self%" in text

    def test_format_has_tier_legend(self, parsed_log):
        text = format_profile(analyze_profile(parsed_log))
        assert "ignition" in text

    def test_total_pct_fields_present(self, parsed_log):
        s = analyze_profile(parsed_log)
        for e in s.entries:
            assert e.total_ticks >= e.self_ticks
            assert e.total_pct >= e.self_pct

    def test_total_pct_in_format(self, parsed_log):
        text = format_profile(analyze_profile(parsed_log))
        assert "total%" in text

    def test_total_counts_nested_stack(self, tmp_path):
        # target is self; outer is its caller — total for target should be 1,
        # and outer (0 self ticks) is not in the profile entries
        log_text = dedent("""\
            code-creation,LazyCompile,0,100,0x1000,200,target t.js:1:1,0x5000,*
            code-creation,LazyCompile,0,100,0x2000,200,outer t.js:2:1,0x6000,*
            tick,0x1050,1000,0,0x0,0,0x2050
        """)
        p = tmp_path / "v8.log"
        p.write_text(log_text)
        log = V8Log.parse(p)
        s = analyze_profile(log)
        by_name = {e.name: e for e in s.entries}
        assert by_name["target"].self_ticks == 1
        assert by_name["target"].total_ticks == 1
        # outer has 0 self ticks — not ranked into entries
        assert "outer" not in by_name


# ══════════════════════════════════════════════════════════════════════════════
# Analysis: vms
# ══════════════════════════════════════════════════════════════════════════════


class TestAnalyzeVms:
    def test_total(self, parsed_log):
        s = analyze_vms(parsed_log)
        assert s.total == 5

    def test_counts(self, parsed_log):
        s = analyze_vms(parsed_log)
        assert s.counts["JS"] == 4
        assert s.counts["GC"] == 1

    def test_format_not_empty(self, parsed_log):
        text = format_vms(analyze_vms(parsed_log))
        assert "JS" in text
        assert "GC" in text
        assert "%" in text


# ══════════════════════════════════════════════════════════════════════════════
# Analysis: fn
# ══════════════════════════════════════════════════════════════════════════════


@pytest.fixture()
def fn_log(tmp_path):
    log_text = dedent("""\
        code-creation,LazyCompile,0,100,0x1000,200,target test.js:1:1,0x5000,~
        code-creation,LazyCompile,0,200,0x2000,200,target test.js:1:1,0x5000,*
        code-creation,LazyCompile,0,100,0x3000,200,caller test.js:10:1,0x6000,~
        code-deopt,500,200,0x2000,0,10,eager,<test.js:2:5>,wrong map
        LoadIC,0x1050,200,5,3,0,N,0xabc,prop,,
        tick,0x1050,1000,0,0x0,0,0x3050
        tick,0x1050,2000,0,0x0,0,0x3050
        tick,0x3050,3000,0,0x0,0
    """)
    p = tmp_path / "v8.log"
    p.write_text(log_text)
    return V8Log.parse(p)


class TestAnalyzeFn:
    def test_compilations(self, fn_log):
        s = analyze_fn(fn_log, "target")
        assert len(s.compilations) == 2
        tiers = [c.tier for c in s.compilations]
        assert "ignition" in tiers
        assert "turbofan" in tiers

    def test_deopts(self, fn_log):
        s = analyze_fn(fn_log, "target")
        assert len(s.deopts) == 1
        assert s.deopts[0].deopt_reason == "wrong map"

    def test_ics(self, fn_log):
        s = analyze_fn(fn_log, "target")
        assert len(s.ics) == 1
        assert s.ics[0].key == "prop"

    def test_self_ticks(self, fn_log):
        s = analyze_fn(fn_log, "target")
        assert s.self_ticks == 2

    def test_total_ticks(self, fn_log):
        s = analyze_fn(fn_log, "target")
        assert s.total_ticks == 2

    def test_callers(self, fn_log):
        s = analyze_fn(fn_log, "target")
        assert len(s.callers) >= 1
        assert s.callers[0][0] == "caller"
        assert s.callers[0][2] == 2  # called twice

    def test_pattern_no_match(self, fn_log):
        s = analyze_fn(fn_log, "nonexistent")
        assert len(s.compilations) == 0

    def test_glob_pattern(self, fn_log):
        s = analyze_fn(fn_log, "tar*")
        assert len(s.compilations) == 2

    def test_format_not_empty(self, fn_log):
        s = analyze_fn(fn_log, "target")
        text = format_fn(s)
        assert "target" in text
        assert "Tier:" in text
        assert "Deopts:" in text

    def test_format_no_match(self, fn_log):
        s = analyze_fn(fn_log, "nonexistent")
        text = format_fn(s)
        assert "No code entries" in text


# ══════════════════════════════════════════════════════════════════════════════
# Formatting: ansi parameter
# ══════════════════════════════════════════════════════════════════════════════


class TestFormattingAnsi:
    """Verify that format functions work with both ansi=True and ansi=False."""

    def test_deopts_ansi(self, deopt_log):
        s = analyze_deopts(deopt_log)
        plain = format_deopts(s, ansi=False)
        ansi = format_deopts(s, ansi=True)
        # Both should contain the same data
        assert "Deopts: 4 total" in plain
        assert "Deopts: 4 total" in ansi

    def test_ics_ansi(self, ic_log):
        s = analyze_ics(ic_log)
        plain = format_ics(s, ansi=False)
        ansi = format_ics(s, ansi=True)
        assert "ICs:" in plain
        assert "ICs:" in ansi

    def test_profile_ansi(self, parsed_log):
        s = analyze_profile(parsed_log)
        plain = format_profile(s, ansi=False)
        ansi = format_profile(s, ansi=True)
        assert "VM state:" in plain
        assert "VM state:" in ansi

    def test_vms_ansi(self, parsed_log):
        s = analyze_vms(parsed_log)
        plain = format_vms(s, ansi=False)
        ansi = format_vms(s, ansi=True)
        assert "JS" in plain
        assert "JS" in ansi


# ══════════════════════════════════════════════════════════════════════════════
# Edge cases
# ══════════════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    def test_empty_log(self, tmp_path):
        p = tmp_path / "v8.log"
        p.write_text("")
        log = V8Log.parse(p)
        assert len(log.ticks) == 0
        assert len(log.deopts) == 0
        assert len(log.ics) == 0

    def test_empty_analyses(self, tmp_path):
        p = tmp_path / "v8.log"
        p.write_text("")
        log = V8Log.parse(p)
        assert analyze_deopts(log).total == 0
        assert analyze_ics(log).total == 0
        assert analyze_maps(log).creates == 0
        assert analyze_profile(log).total_ticks == 0
        assert analyze_vms(log).total == 0
        assert len(analyze_fn(log, "*").compilations) == 0

    def test_empty_format(self, tmp_path):
        p = tmp_path / "v8.log"
        p.write_text("")
        log = V8Log.parse(p)
        # Should not crash on empty data
        format_deopts(analyze_deopts(log))
        format_ics(analyze_ics(log))
        format_maps(analyze_maps(log))
        format_profile(analyze_profile(log))
        format_vms(analyze_vms(log))
        format_fn(analyze_fn(log, "*"))

    def test_sfi_move(self, tmp_path):
        log_text = dedent("""\
            code-creation,LazyCompile,0,100,0x1000,200,fn test.js:1:1,0x5000,~
            sfi-move,0x5000,0x6000
        """)
        p = tmp_path / "v8.log"
        p.write_text(log_text)
        log = V8Log.parse(p)
        assert log._sfi_map[0x5000] == 0x6000

    def test_code_delete(self, tmp_path):
        log_text = dedent("""\
            code-creation,LazyCompile,0,100,0x1000,200,fn test.js:1:1,0x5000,~
            code-delete,0x1000
        """)
        p = tmp_path / "v8.log"
        p.write_text(log_text)
        log = V8Log.parse(p)
        assert log.code_map.lookup(0x1050) is None

    def test_regexp_no_state(self, tmp_path):
        log_text = "code-creation,RegExp,0,100,0x1000,200,/foo/\n"
        p = tmp_path / "v8.log"
        p.write_text(log_text)
        log = V8Log.parse(p)
        entry = log.code_map.lookup(0x1050)
        assert entry is not None
        assert entry.type == "RegExp"
        assert entry.state == ""


# ══════════════════════════════════════════════════════════════════════════════
# CppSymbolizer
# ══════════════════════════════════════════════════════════════════════════════


class TestCppSymbolizer:
    def test_symbolize_missing_binary(self):
        """Symbolizer should silently skip missing binaries."""
        lib = SharedLibrary("/nonexistent/binary", 0x1000, 0x2000, 0)
        cm = CodeMap()
        CppSymbolizer([lib]).symbolize_into(cm)
        assert cm.lookup(0x1500) is None

    def test_symbolize_into_codemap(self, tmp_path):
        """Symbolizer should populate code map from nm output."""
        # Create a tiny C program and compile it so nm has something to parse
        src = tmp_path / "test.c"
        src.write_text(
            "int myfunc(int x) { return x + 1; }\nint main() { return myfunc(0); }\n"
        )
        binary = tmp_path / "test_bin"
        import subprocess

        result = subprocess.run(
            ["cc", "-o", str(binary), str(src)],
            capture_output=True,
        )
        if result.returncode != 0:
            pytest.skip("cc not available")

        # Find the address of myfunc via nm
        nm_result = subprocess.run(
            ["nm", "-n", str(binary)],
            capture_output=True,
            text=True,
        )
        myfunc_offset = None
        for line in nm_result.stdout.splitlines():
            if "myfunc" in line:
                myfunc_offset = int(line.split()[0], 16)
                break
        if myfunc_offset is None:
            pytest.skip("Could not find myfunc in nm output")

        # Simulate the binary loaded at address 0x400000
        lib_start = 0x400000
        lib = SharedLibrary(str(binary), lib_start, lib_start + 0x100000, 0)
        cm = CodeMap()
        CppSymbolizer([lib]).symbolize_into(cm)

        # myfunc should now be in the code map
        entry = cm.lookup(lib_start + myfunc_offset)
        assert entry is not None
        assert entry.type == "CPP"
        assert "myfunc" in entry.name

    def test_symbolize_idempotent(self, tmp_path):
        """V8Log.symbolize() should only run once."""
        p = tmp_path / "v8.log"
        p.write_text("shared-library,/nonexistent,0x1000,0x2000,0\n")
        log = V8Log.parse(p)
        log.symbolize()
        assert log._symbolized is True
        # Second call should be a no-op (no error even though binary doesn't exist)
        log.symbolize()

    def test_profile_triggers_symbolize(self, tmp_path):
        """analyze_profile should auto-trigger symbolization."""
        p = tmp_path / "v8.log"
        p.write_text("tick,0x1000,100,0,0x0,0\n")
        log = V8Log.parse(p)
        assert log._symbolized is False
        analyze_profile(log)
        assert log._symbolized is True

    def test_fn_triggers_symbolize(self, tmp_path):
        """analyze_fn should auto-trigger symbolization."""
        p = tmp_path / "v8.log"
        p.write_text(
            "code-creation,LazyCompile,0,100,0x1000,200,fn test.js:1:1,0x5000,~\n"
        )
        log = V8Log.parse(p)
        assert log._symbolized is False
        analyze_fn(log, "fn")
        assert log._symbolized is True
