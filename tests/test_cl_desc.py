import json
import subprocess
import sys

import pytest

from v8_utils.cli.cl_desc import GUARD, Invalid, install_hook, validate

CID = "Ia6e5bee528a3c72b728b390c1e684655d4600a0a"
GOOD = f"[parser] Do a thing\n\nBecause.\n\nChange-Id: {CID}\n"
ENV = {"TMPDIR": "/private/tmp/aisan-501/tmp-x", "HOME": "/Users/someone"}


def test_accepts_well_formed():
    assert validate(GOOD, CID, ENV) == GOOD
    assert validate(GOOD.rstrip("\n") + "\n\n\n", CID, ENV) == GOOD


@pytest.mark.parametrize(
    "text, reason",
    [
        # A file name where the text belongs.
        ("/private/tmp/aisan-501/tmp-x/desc.txt\n", "subject"),
        ("$TMPDIR/desc.txt\n", "subject"),
        ("[parser] Do a thing\n", "subject line"),
        ("[parser] Do a thing\nBody.\n", "subject line"),
        (GOOD.replace("Because.", "See /private/tmp/x/notes."), "local path"),
        (GOOD.replace("Because.", "See ~/notes."), "local path"),
        (GOOD.replace("Because.", "Built in /Users/someone/v8."), "local path"),
        (GOOD.replace("Because.", "Because → faster."), "non-ASCII"),
        (GOOD.replace(f"Change-Id: {CID}\n", ""), "found 0"),
        (GOOD + f"Change-Id: {CID}\n", "found 2"),
        (GOOD.replace(CID, "I" + "0" * 40), "is not the CL's"),
    ],
)
def test_rejects(text, reason):
    with pytest.raises(Invalid, match=reason):
        validate(text, CID, ENV)


def test_rejects_env_paths_without_known_prefix():
    env = {"TMPDIR": "/scratch/agent-tmp", "HOME": ""}
    with pytest.raises(Invalid, match=r"\$TMPDIR"):
        validate(GOOD.replace("Because.", "Log in /scratch/agent-tmp."), CID, env)


def guard_denies(command):
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    out = subprocess.run(
        [sys.executable, "-c", GUARD],
        input=payload,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    if not out:
        return False
    return json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize(
    "command",
    [
        "git cl desc -n $TMPDIR/desc.txt",
        'cd ~/src/v8/x && git cl desc -n "$(cat f)"',
        "git cl description --new-description=foo",
        "git cl desc -f -n -",
        """python3 -c 'subprocess.run(["git", "cl", "desc", "-n", p])'""",
        "git cl upload --commit-description=- < f",
        'git cl upload -m "msg"',
        "git cl upload --message-file f",
        "curl -X PUT https://host/a/changes/1/message -d @f",
        "curl -X PUT https://host/a/changes/1/edit:message -d @f",
    ],
)
def test_guard_denies(command):
    assert guard_denies(command)


@pytest.mark.parametrize(
    "command",
    [
        "git cl desc -d",
        'git cl upload -t "Address review comments" --bypass-hooks',
        "git cl issue; ls -n",
        "git log -n 3",
        "curl -s https://host/changes/1/revisions/current/commit",
        "cl-set-desc $TMPDIR/d.txt --dry-run",
    ],
)
def test_guard_allows(command):
    assert not guard_denies(command)


def test_install_hook(tmp_path):
    settings = tmp_path / "settings.json"
    edit_hook = {"type": "command", "command": "true"}
    settings.write_text(
        json.dumps(
            {
                "model": "x",
                "hooks": {"PreToolUse": [{"matcher": "Edit", "hooks": [edit_hook]}]},
            }
        )
    )
    assert install_hook(settings)
    assert not install_hook(settings)
    data = json.loads(settings.read_text())
    assert data["model"] == "x"
    groups = data["hooks"]["PreToolUse"]
    assert [g["matcher"] for g in groups] == ["Edit", "Bash"]
    assert groups[1]["hooks"][0]["args"] == ["-c", GUARD]
