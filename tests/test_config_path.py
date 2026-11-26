"""Which config file v8-utils reads: the user's, or an override."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from v8_utils import config, dirs
from v8_utils.api import config as api_config


def test_environment_override_is_read_at_import(tmp_path):
    cfg = tmp_path / "other.toml"
    cfg.write_text('user = "bot@example.com"\n')
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from v8_utils.api import config; "
                "print(config.config_path()); print(config.load().user)"
            ),
        ],
        env={**os.environ, config.CONFIG_ENV: str(cfg)},
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.split() == [str(cfg), "bot@example.com"]


def test_configure_switches_the_file_and_drops_the_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_PATH", config.CONFIG_PATH)
    monkeypatch.setattr(config, "_cache", None)
    first = tmp_path / "a.toml"
    first.write_text('user = "a@example.com"\n')
    second = tmp_path / "b.toml"
    second.write_text('user = "b@example.com"\n')
    api_config.configure(first)
    assert api_config.load().user == "a@example.com"
    api_config.configure(second)
    assert api_config.config_path() == second
    assert api_config.load().user == "b@example.com"


def test_configure_expands_the_home_directory(monkeypatch):
    monkeypatch.setattr(config, "CONFIG_PATH", config.CONFIG_PATH)
    monkeypatch.setattr(config, "_cache", None)
    api_config.configure(Path("~/x.toml"))
    assert api_config.config_path() == Path.home() / "x.toml"


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_dirs_are_xdg_on_every_platform(monkeypatch, tmp_path, platform):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(var, raising=False)
    assert dirs.config_dir() == tmp_path / ".config" / "v8-utils"
    assert dirs.data_dir() == tmp_path / ".local" / "share" / "v8-utils"
    assert dirs.cache_dir() == tmp_path / ".cache" / "v8-utils"


def test_dirs_honour_xdg_variables(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "c"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "d"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "k"))
    assert dirs.config_dir() == tmp_path / "c" / "v8-utils"
    assert dirs.data_dir() == tmp_path / "d" / "v8-utils"
    assert dirs.cache_dir() == tmp_path / "k" / "v8-utils"
