"""Fenced code blocks in Markdown, as the doc tests read them.

A fence opens with three or more backticks or tildes and closes with a line of the same
character, at least as long. The tag is the first word after the opening fence (`sh` in
```` ```sh ````), or empty.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterator
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
# Blocks that hold a file's contents or output, which nobody pastes into a shell.
NOT_PASTED = frozenset({"text", "json", "yaml"})

_FENCE = re.compile(r"^\s*(`{3,}|~{3,})\s*([^\s`]*)")


def markdown_files() -> list[str]:
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "-C", str(REPO), "ls-files", "*.md"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    names = done.stdout.split()
    assert names
    return names


def code_lines(text: str) -> Iterator[tuple[int, str, int, str]]:
    """(line number, fence tag, line index within its block, line) for each line in a block."""
    fence = None
    tag = ""
    index = 0
    for number, line in enumerate(text.splitlines(), 1):
        if fence is None:
            opened = _FENCE.match(line)
            if opened:
                fence, tag, index = opened.group(1), opened.group(2).lower(), 0
            continue
        stripped = line.strip()
        if stripped and set(stripped) == {fence[0]} and len(stripped) >= len(fence):
            fence = None
            continue
        yield number, tag, index, line
        index += 1
