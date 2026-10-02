"""Every shipped CLI exposes the bundled release stamp without loading config."""

import os
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"]


@pytest.mark.parametrize("name,entry", SCRIPTS.items())
def test_version(name, entry, tmp_path):
    cfg = tmp_path / "invalid.toml"
    cfg.write_text("this is not TOML")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import importlib, sys; "
            "module, attr = sys.argv.pop(1).split(':'); "
            "sys.argv.pop(0); getattr(importlib.import_module(module), attr)()",
            entry,
            name,
            "--version",
        ],
        env={**os.environ, "V8_UTILS_CONFIG": str(cfg)},
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{name} {(ROOT / 'VERSION').read_text().strip()}\n"
    assert result.stderr == ""


def test_pd_typer_version():
    from typer.testing import CliRunner
    from v8_utils.cli.pd import app

    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
    assert result.stdout == f"pd {(ROOT / 'VERSION').read_text().strip()}\n"
