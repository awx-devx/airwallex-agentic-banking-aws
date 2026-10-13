"""Shell commands in the Markdown docs work when pasted into bash or zsh.

zsh is the default macOS shell. An interactive zsh does not word-split an unquoted `$VAR`, so
a command held in a string variable fails there, and by default it does not treat `#` as a
comment, so a comment on a pasted command line becomes arguments.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SHELL_FENCES = {"", "sh", "bash", "shell", "zsh"}


def _markdown_files() -> list[str]:
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "-C", str(REPO), "ls-files", "*.md"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    names = done.stdout.split()
    assert names
    return names


def _shell_lines():
    """(file, line number, line) for each line of a shell code block, outside heredocs."""
    for name in _markdown_files():
        fence = None
        heredoc = None
        for number, line in enumerate((REPO / name).read_text().splitlines(), 1):
            if heredoc is not None:
                if line.strip() == heredoc:
                    heredoc = None
                continue
            if line.lstrip().startswith("```"):
                fence = None if fence is not None else line.strip()[3:]
                continue
            if fence is None or fence not in SHELL_FENCES:
                continue
            opened = re.search(r"<<-?'?(\w+)'?", line)
            if opened:
                heredoc = opened.group(1)
            yield name, number, line


def test_shell_blocks_have_no_comments():
    found = [
        f"{name}:{number}: {line.strip()}"
        for name, number, line in _shell_lines()
        if re.search(r"(^|\s)#(\s|$)", line)
    ]
    assert not found, "comments in shell blocks (zsh runs them as arguments):\n" + "\n".join(found)


def test_no_command_is_held_in_a_string_variable():
    assign = re.compile(r"^\s*(?:export\s+)?([A-Za-z_]\w*)=[\"']?[a-z][\w.-]*\s+-")
    found = [
        f"{name}:{number}: {line.strip()}"
        for name, number, line in _shell_lines()
        if assign.match(line)
    ]
    assert not found, "commands in string variables (zsh does not word-split them):\n" + (
        "\n".join(found)
    )


def _demo_user_block() -> str:
    text = (REPO / "docs" / "deployment.md").read_text()
    (block,) = [b for b in re.findall(r"```\n(.*?)```", text, re.S) if "create_demo_user()" in b]
    lines = [line for line in block.splitlines() if not line.startswith("POOL=")]
    calls = [line for line in lines if line.startswith("create_demo_user ")][:1]
    body = [line for line in lines if not line.startswith("create_demo_user ")]
    return "\n".join([*body, *calls]) + "\n"


@pytest.mark.parametrize("shell", ["bash", "zsh"])
@pytest.mark.parametrize("region", ["ap-southeast-2", None], ids=["set", "unset"])
def test_the_demo_user_block_runs_and_stops_with_no_region(tmp_path, shell, region):
    if shutil.which(shell) is None:
        pytest.skip(f"{shell} is not installed")
    calls = tmp_path / "calls.log"
    # aws and python3 are stubbed: the block's python3 writes the temporary password files.
    stubs = (
        "POOL=pool-123\n"
        f'aws() {{ echo "aws $*" >> {calls}; }}\n'
        f'python3() {{ echo "python3" >> {calls}; cat > /dev/null; }}\n'
    )
    env = {k: v for k, v in os.environ.items() if k != "REGION"}
    env["HOME"] = str(tmp_path)
    if region:
        env["REGION"] = region
    argv = ["bash", "--norc", "--noprofile", "-i"] if shell == "bash" else ["zsh", "-f", "-i"]
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell string
        argv, input=stubs + _demo_user_block(), capture_output=True, text=True, env=env
    )
    made = calls.read_text().splitlines() if calls.exists() else []
    if region:
        assert made[0] == "python3"
        aws_calls = [c for c in made if c.startswith("aws ")]
        assert len(aws_calls) == 4
        assert all(c.startswith(f"aws --profile awx-deploy --region {region} ") for c in aws_calls)
        assert "awx-poc/demo-users/demo-platform-requester" in aws_calls[0]
    else:
        assert made == []
        assert "set REGION first, for example: export REGION=us-west-2" in done.stderr
