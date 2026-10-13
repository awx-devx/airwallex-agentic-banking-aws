"""Shell commands in the Markdown docs work when pasted into bash or zsh.

zsh is the default macOS shell. An interactive zsh does not word-split an unquoted `$VAR`, so
a command held in a string variable fails there. By default it does not treat `#` as a
comment, so a comment on a pasted command line becomes arguments, and it reads `read -p` as
reading from a coprocess.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load_markdown_blocks():
    path = Path(__file__).resolve().parent / "markdown_blocks.py"
    spec = importlib.util.spec_from_file_location("_awx_markdown_blocks", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


blocks = _load_markdown_blocks()


def _pasted_lines(text: str):
    """(line number, block index, line) for each line of a pasted block, outside heredocs."""
    heredoc = None
    for number, tag, index, line in blocks.code_lines(text):
        if tag in blocks.NOT_PASTED:
            continue
        if index == 0:
            heredoc = None
        if heredoc is not None:
            if line.strip() == heredoc:
                heredoc = None
            continue
        opened = re.search(r"<<-?'?(\w+)'?", line)
        if opened:
            heredoc = opened.group(1)
        yield number, index, line


def _comments(text: str) -> list[str]:
    return [
        f"{number}: {line.strip()}"
        for number, index, line in _pasted_lines(text)
        if re.search(r"(^|\s)#", line) and not (index == 0 and line.startswith("#!"))
    ]


def _prompting_reads(text: str) -> list[str]:
    # zsh reads `read -p` as "read from the coprocess", so the variable stays unset.
    return [
        f"{number}: {line.strip()}"
        for number, _, line in _pasted_lines(text)
        if re.search(r"(^|[\s;&|(])read\s+(-\w*\s+)*-\w*p", line)
    ]


def _string_commands(text: str) -> list[str]:
    assign = re.compile(r"^\s*(?:export\s+)?([A-Za-z_]\w*)=[\"']?[a-z][\w.-]*\s+-")
    return [
        f"{number}: {line.strip()}" for number, _, line in _pasted_lines(text) if assign.match(line)
    ]


def _every_file(check) -> list[str]:
    found = []
    for name in blocks.markdown_files():
        found += [f"{name}:{hit}" for hit in check((REPO / name).read_text())]
    return found


def test_pasted_blocks_have_no_comments():
    found = _every_file(_comments)
    assert not found, "comments in pasted blocks (zsh runs them as arguments):\n" + "\n".join(found)


def test_pasted_blocks_have_no_prompting_read():
    found = _every_file(_prompting_reads)
    assert not found, "read -p in pasted blocks (zsh reads a coprocess):\n" + "\n".join(found)


def test_no_command_is_held_in_a_string_variable():
    found = _every_file(_string_commands)
    assert not found, "commands in string variables (zsh does not word-split them):\n" + (
        "\n".join(found)
    )


@pytest.mark.parametrize(
    ("block", "caught"),
    [
        ('```\nmake deploy REGION="$REGION" # once\n```\n', True),
        ('```\nmake deploy REGION="$REGION" #once\n```\n', True),
        ("~~~sh\nmake deploy\n# c\n~~~\n", True),
        ("```console\n$ make check  # c\n```\n", True),
        ("  ```bash\n  ls # c\n  ```\n", True),
        ("```sh\n#!/bin/sh\nls\n```\n", False),
        ("```\nls\n#!/bin/sh\n```\n", True),
        ("```text\nKEY=value # c\n```\n", False),
        ("```yaml\nkey: value # c\n```\n", False),
        ("````\n```\n# inside a longer fence\n````\n", True),
        ("Prose with a # sign.\n", False),
        ("```\necho ${#name} https://example.com/#top\n```\n", False),
    ],
)
def test_the_comment_check_reads_every_pasted_block(block, caught):
    assert bool(_comments(block)) is caught


@pytest.mark.parametrize(
    ("block", "caught"),
    [
        ('```\nread -rsp "Key: " KEY\n```\n', True),
        ('```\nread -p "Key: " KEY\n```\n', True),
        ('```zsh\nx=1; read -r -p "Key: " KEY\n```\n', True),
        ("~~~\nread -rs KEY\n~~~\n", False),
        ("```\nIFS= read -r KEY\n```\n", False),
        ("```text\nread -p\n```\n", False),
    ],
)
def test_the_prompting_read_check(block, caught):
    assert bool(_prompting_reads(block)) is caught


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
    # No AWS credential or configuration reaches the shells, and an `aws` on PATH only records
    # that it ran, so a stub that failed to define calls no real AWS CLI.
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    (stub_bin / "aws").write_text(f'#!/bin/sh\necho "real aws $*" >> {calls}\n')
    (stub_bin / "aws").chmod(0o700)
    env = {k: v for k, v in os.environ.items() if k != "REGION" and not k.startswith("AWS_")}
    env["AWS_CONFIG_FILE"] = os.devnull
    env["AWS_SHARED_CREDENTIALS_FILE"] = os.devnull
    env["PATH"] = f"{stub_bin}{os.pathsep}{env.get('PATH', '')}"
    env["HOME"] = str(tmp_path)
    if region:
        env["REGION"] = region
    argv = ["bash", "--norc", "--noprofile", "-i"] if shell == "bash" else ["zsh", "-f", "-i"]
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell string
        argv, input=stubs + _demo_user_block(), capture_output=True, text=True, env=env
    )
    made = calls.read_text().splitlines() if calls.exists() else []
    assert not [c for c in made if c.startswith("real aws")]
    if region:
        assert made[0] == "python3"
        aws_calls = [c for c in made if c.startswith("aws ")]
        assert len(aws_calls) == 4
        assert all(c.startswith(f"aws --profile awx-deploy --region {region} ") for c in aws_calls)
        assert "awx-poc/demo-users/demo-platform-requester" in aws_calls[0]
    else:
        assert made == []
        assert "set REGION first, for example: export REGION=us-west-2" in done.stderr
