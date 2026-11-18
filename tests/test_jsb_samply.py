"""jsb recording with samply, against a fake samply on PATH."""

import json
import sys
import textwrap
from pathlib import Path

import pytest

from v8_utils import config, jsb
from v8_utils.jsb import Variant, resolve_profiler, run_samply

# Stands in for `samply record --save-only -o OUT -- d8 ...`: writes what samply
# and d8 would, into the paths the command names, and logs its argv and cwd.
FAKE_SAMPLY = """\
#!{python}
import json, os, sys
args = sys.argv[1:]
if args == ["--version"]:
    print("samply 0.13.1")
    sys.exit(0)
log = os.environ["FAKE_SAMPLY_LOG"]
with open(log, "w") as f:
    json.dump({{"argv": args, "cwd": os.getcwd()}}, f)
mode = os.environ.get("FAKE_SAMPLY_MODE", "ok")
if mode == "fail":
    print("d8 crashed", file=sys.stderr)
    sys.exit(3)
out = args[args.index("-o") + 1]
open(out, "wb").close()
if mode != "no-jitdump":
    jit_dir = next(a.split("=", 1)[1] for a in args if a.startswith("--perf-prof-path="))
    open(os.path.join(jit_dir, "jit-123.dump"), "wb").close()
print("Score 42")
"""


@pytest.fixture
def fake_samply(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    samply = bin_dir / "samply"
    samply.write_text(FAKE_SAMPLY.format(python=sys.executable))
    samply.chmod(0o755)
    log = tmp_path / "samply-call.json"
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_SAMPLY_LOG", str(log))
    return log


@pytest.fixture
def suite(tmp_path):
    suite_dir = tmp_path / "JetStream3"
    suite_dir.mkdir()
    (suite_dir / "cli.js").write_text("")
    d8 = tmp_path / "out" / "release" / "d8"
    d8.parent.mkdir(parents=True)
    d8.write_text("")
    return suite_dir, d8


def _record(suite, tmp_path, lineitems=("regexp-octane",)) -> Path:
    suite_dir, d8 = suite
    return run_samply(
        Variant.parse(f"{d8}:--turbolev"),
        suite_dir,
        list(lineitems),
        d8.parent.parent,
        tmp_path / "recordings",
    )


@pytest.mark.parametrize(
    ("platform", "expected"), [("darwin", "samply"), ("linux", "linux-perf")]
)
def test_auto_profiler_follows_platform(monkeypatch, platform, expected):
    monkeypatch.setattr(sys, "platform", platform)
    assert resolve_profiler("auto") == expected


def test_explicit_profiler_wins_over_platform(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert resolve_profiler("linux-perf") == "linux-perf"


def test_unknown_profiler_names_the_config_key():
    with pytest.raises(ValueError, match="config key `profiler`"):
        resolve_profiler("xctrace")


def test_recording_dir_is_self_contained(fake_samply, suite, tmp_path):
    suite_dir, d8 = suite
    rec = _record(suite, tmp_path)

    assert rec.parent == tmp_path / "recordings"
    assert rec.name.startswith("regexp-octane-")
    for name in ("profile.json.gz", "jit-123.dump", "meta.json", "output.txt"):
        assert (rec / name).is_file(), name
    assert "Score 42" in (rec / "output.txt").read_text()

    call = json.loads(fake_samply.read_text())
    assert call["cwd"] == str(suite_dir)
    argv = call["argv"]
    assert argv[:4] == ["record", "--save-only", "-o", str(rec / "profile.json.gz")]
    d8_cmd = argv[argv.index("--") + 1 :]
    assert d8_cmd[0] == str(d8)
    # d8 runs with cwd=suite_dir, so every output path must point into rec.
    assert f"--perf-prof-path={rec}" in d8_cmd
    assert f"--logfile={rec / 'v8.log'}" in d8_cmd
    assert "--turbolev" in d8_cmd
    assert d8_cmd[-3:] == [str(suite_dir / "cli.js"), "--", "regexp-octane"]

    meta = json.loads((rec / "meta.json").read_text())
    assert meta["d8_cmd"] == d8_cmd
    assert meta["samply_version"] == "samply 0.13.1"
    assert meta["lineitems"] == ["regexp-octane"]


def test_recordings_do_not_collide(fake_samply, suite, tmp_path):
    assert _record(suite, tmp_path) != _record(suite, tmp_path)


def test_missing_samply_names_the_install(monkeypatch, suite, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(FileNotFoundError, match="brew install samply"):
        _record(suite, tmp_path)


def test_failed_run_keeps_output(fake_samply, monkeypatch, suite, tmp_path):
    monkeypatch.setenv("FAKE_SAMPLY_MODE", "fail")
    with pytest.raises(RuntimeError, match="exit 3") as exc:
        _record(suite, tmp_path)
    (rec,) = (tmp_path / "recordings").iterdir()
    assert str(rec) in str(exc.value)
    assert "d8 crashed" in (rec / "output.txt").read_text()


def test_missing_jitdump_is_an_error(fake_samply, monkeypatch, suite, tmp_path):
    monkeypatch.setenv("FAKE_SAMPLY_MODE", "no-jitdump")
    with pytest.raises(RuntimeError, match=r"jit-<pid>\.dump"):
        _record(suite, tmp_path)


@pytest.fixture
def samply_config(tmp_path, suite):
    suite_dir, _ = suite
    path = tmp_path / "config.toml"
    path.write_text(
        textwrap.dedent(
            f"""\
            profiler = "samply"
            recordings_dir = "{tmp_path / "recordings"}"
            [repos.js3]
            path = "{suite_dir}"
            """
        )
    )
    original = config.CONFIG_PATH
    config.configure(path)
    yield
    config.configure(original)


def test_jsb_record_perf_dispatches_to_samply(
    fake_samply, samply_config, suite, tmp_path
):
    _, d8 = suite
    rec = Path(jsb.jsb_record(["regexp-octane"], [str(d8)], "js3", "perf"))
    assert rec.parent == tmp_path / "recordings"
    assert (rec / "profile.json.gz").is_file()


def test_perf_upload_is_refused_with_samply(fake_samply, samply_config, suite):
    _, d8 = suite
    with pytest.raises(ValueError, match="perf_upload needs linux-perf"):
        jsb.jsb_record(["regexp-octane"], [str(d8)], "js3", "perf_upload")
    assert not fake_samply.exists()
