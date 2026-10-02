"""Update checks must not install without consent or interfere with CLI work."""

import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from v8_utils import config, update


class Terminal(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def startup(tmp_path, monkeypatch):
    monkeypatch.delenv("V8_UTILS_NO_AUTO_UPDATE", raising=False)
    monkeypatch.setattr(
        update,
        "sys",
        SimpleNamespace(
            stdin=Terminal("yes\n"),
            stdout=Terminal(),
            stderr=Terminal(),
        ),
    )
    monkeypatch.setattr(config, "load", lambda: config.Config())
    monkeypatch.setattr(update, "user_cache_dir", lambda _: str(tmp_path))
    monkeypatch.setattr(update, "_eligible_install", Mock(return_value=True))
    monkeypatch.setattr(update, "current_version", lambda: 10)
    monkeypatch.setattr(update, "latest_version", Mock(return_value=11))
    monkeypatch.setattr(
        update.subprocess, "run", Mock(return_value=SimpleNamespace(returncode=0))
    )
    return tmp_path


def test_accept_updates_and_exits_before_dispatch(startup):
    with pytest.raises(SystemExit) as exc:
        update.check_for_update()
    assert exc.value.code == 0
    update.subprocess.run.assert_called_once_with(
        [
            "uv",
            "tool",
            "install",
            "git+https://github.com/schuay/v8-utils.git",
            "--reinstall",
        ],
        stdout=update.sys.stderr,
        stderr=update.sys.stderr,
    )
    assert update.sys.stdout.getvalue() == ""
    assert "Rerun your command" in update.sys.stderr.getvalue()
    assert json.loads((startup / "update.json").read_text())["attempted"] == 11


@pytest.mark.parametrize("latest", [9, 10])
def test_only_newer_promotions_prompt(startup, latest):
    update.latest_version.return_value = latest
    update.check_for_update()
    update.subprocess.run.assert_not_called()
    assert update.sys.stderr.getvalue() == ""


@pytest.mark.parametrize("answer", ["n\n", "\n", "", "sure\n"])
def test_decline_and_cache(startup, answer):
    update.sys.stdin = Terminal(answer)
    update.check_for_update()
    update.check_for_update()
    update.latest_version.assert_called_once()
    update.subprocess.run.assert_not_called()
    assert "attempted" not in json.loads((startup / "update.json").read_text())


@pytest.mark.parametrize(
    "switch", ["env", "config", "source", "stdin", "stdout", "stderr"]
)
def test_skip_before_network(startup, monkeypatch, switch):
    if switch == "env":
        monkeypatch.setenv("V8_UTILS_NO_AUTO_UPDATE", "1")
        monkeypatch.setattr(
            config, "load", Mock(side_effect=AssertionError("read config"))
        )
    elif switch == "config":
        monkeypatch.setattr(config, "load", lambda: config.Config(auto_update=False))
    elif switch == "source":
        update._eligible_install.return_value = False
    else:
        monkeypatch.setattr(update.sys, switch, io.StringIO())
    update.check_for_update()
    update.latest_version.assert_not_called()
    update.subprocess.run.assert_not_called()
    assert not (startup / "update.json").exists()


@pytest.mark.parametrize(
    "error",
    [httpx.ConnectError("offline"), ValueError("bad VERSION"), OSError("cache")],
)
def test_check_failure_continues(startup, error):
    update.latest_version.side_effect = error
    update.check_for_update()
    update.subprocess.run.assert_not_called()
    assert not (startup / "update.json").exists()


@pytest.mark.parametrize("failure", [1, FileNotFoundError("uv")])
def test_failed_install_exits_and_is_not_retried(startup, monkeypatch, failure):
    if isinstance(failure, Exception):
        update.subprocess.run.side_effect = failure
    else:
        update.subprocess.run.return_value.returncode = failure
    with pytest.raises(SystemExit) as exc:
        update.check_for_update()
    assert exc.value.code == 1
    monkeypatch.setattr(update.time, "time", lambda: 10**12)
    update.check_for_update()
    assert update.subprocess.run.call_count == 1
    assert "Retry with `pp upgrade`" in update.sys.stderr.getvalue()


def test_lock_skips_concurrent_startup(startup):
    with (startup / "update.lock").open("a") as lock:
        update.fcntl.flock(lock, update.fcntl.LOCK_EX | update.fcntl.LOCK_NB)
        update.check_for_update()
    update.latest_version.assert_not_called()


def test_expired_or_corrupt_cache_checks_again(startup):
    (startup / "update.json").write_text('{"checked_at": "invalid"}')
    update.latest_version.return_value = 10
    update.check_for_update()
    state = json.loads((startup / "update.json").read_text())
    state["checked_at"] -= update.CHECK_INTERVAL + 1
    (startup / "update.json").write_text(json.dumps(state))
    update.check_for_update()
    assert update.latest_version.call_count == 2


@pytest.mark.parametrize(
    "body", [b"12\n", b"garbage", b"-1", b"1.0", b"9" * 100, b"\xff"]
)
def test_remote_version_parsing(monkeypatch, body):
    stream = Mock(
        return_value=httpx.Response(
            200, content=body, request=httpx.Request("GET", update.VERSION_URL)
        )
    )
    monkeypatch.setattr(update.httpx, "stream", stream)
    # Response is closed by the same context manager protocol as httpx.stream.
    from contextlib import nullcontext

    stream.return_value = nullcontext(stream.return_value)
    if body == b"12\n":
        assert update.latest_version() == 12
    else:
        with pytest.raises(ValueError):
            update.latest_version()
    stream.assert_called_once_with("GET", update.VERSION_URL, timeout=2.0)


@pytest.mark.parametrize(
    "source,eligible",
    [
        (None, True),
        ({"url": update.REPOSITORY, "vcs_info": {"vcs": "git"}}, True),
        (
            {
                "url": update.REPOSITORY,
                "vcs_info": {"vcs": "git", "requested_revision": "main"},
            },
            True,
        ),
        (
            {
                "url": update.REPOSITORY,
                "vcs_info": {"vcs": "git", "requested_revision": "abc123"},
            },
            False,
        ),
        ({"url": "file:///checkout", "dir_info": {}}, False),
        ({"url": "file:///checkout", "dir_info": {"editable": True}}, False),
        (
            {"url": "https://github.com/other/repo.git", "vcs_info": {"vcs": "git"}},
            False,
        ),
    ],
)
def test_provenance(tmp_path, monkeypatch, source, eligible):
    monkeypatch.setattr(update.sys, "prefix", str(tmp_path))
    (tmp_path / "uv-receipt.toml").touch()
    dist = SimpleNamespace(
        read_text=lambda _: None if source is None else json.dumps(source),
        locate_file=lambda _: update.__file__,
    )
    monkeypatch.setattr(update.metadata, "distribution", lambda _: dist)
    assert update._eligible_install() is eligible
    (tmp_path / "uv-receipt.toml").unlink()
    assert not update._eligible_install()


def test_config_template_is_valid_and_disable_loads(tmp_path, monkeypatch):
    import tomllib

    assert tomllib.loads(config.template())["auto_update"] is True
    cfg = tmp_path / "config.toml"
    cfg.write_text("auto_update = false\n")
    monkeypatch.setattr(config, "CONFIG_PATH", cfg)
    monkeypatch.setattr(config, "_cache", None)
    assert config.load().auto_update is False


def test_source_version_matches_root():
    root = Path(__file__).resolve().parents[1]
    assert update.current_version() == int((root / "VERSION").read_text())


@pytest.mark.parametrize("cli", ["pp", "vt"])
def test_startup_exits_before_command_after_update(startup, monkeypatch, cli):
    from v8_utils.cli import pp, vt

    command = Mock()
    if cli == "pp":
        monkeypatch.setattr(pp.sys, "argv", ["pp", "config"])
        monkeypatch.setattr(pp, "_cmd_config", command)
        main = pp.main
    else:
        monkeypatch.setattr(vt, "_cmd_list", command)

        def main():
            vt.main(["list"])

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    update.subprocess.run.assert_called_once()
    command.assert_not_called()


def test_vt_json_skips_check(monkeypatch):
    from v8_utils.cli import vt

    check = Mock(side_effect=AssertionError("update check in JSON mode"))
    command = Mock()
    monkeypatch.setattr(update, "check_for_update", check)
    monkeypatch.setattr(config, "load", lambda: config.Config())
    monkeypatch.setattr(vt, "_cmd_create", command)
    vt.main(["create", "example", "--json"])
    command.assert_called_once()


def test_manual_upgrade_bypasses_check(monkeypatch):
    from v8_utils.cli import pp
    from v8_utils.api import changelog

    monkeypatch.setattr(pp.sys, "argv", ["pp", "upgrade"])
    monkeypatch.setattr(changelog, "show_unseen", lambda: None)
    check = Mock(side_effect=AssertionError("automatic check during manual upgrade"))
    command = Mock()
    monkeypatch.setattr(update, "check_for_update", check)
    monkeypatch.setattr(pp, "_cmd_upgrade", command)
    pp.main()
    command.assert_called_once()
