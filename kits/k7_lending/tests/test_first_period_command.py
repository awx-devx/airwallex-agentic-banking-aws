"""The K7_FIRST_PERIOD command in the README and deploy/k7.yaml prints the demo period running
now, by the same anchor the book uses."""

from __future__ import annotations

import re
import subprocess
import sys
import time
from datetime import UTC
from pathlib import Path

from awx_k7_lending.book import DEMO_ANCHOR, week_window

KIT = Path(__file__).resolve().parents[1]
REPO = KIT.parents[1]
COMMAND = re.compile(r"""python3 -c "(import time; print\(f'DEMO300S-.*?'\))\"""")


def _command(path: Path) -> str:
    found = COMMAND.findall(path.read_text())
    assert len(found) == 1, (path, found)
    return found[0]


def test_readme_and_deploy_file_give_the_same_command():
    assert _command(KIT / "README.md") == _command(REPO / "deploy" / "k7.yaml")


def test_the_command_prints_the_period_running_now():
    assert DEMO_ANCHOR.timestamp() == 1767225600
    before = time.time()
    printed = subprocess.run(  # noqa: S603 - a fixed program from the repo's own docs
        [sys.executable, "-c", _command(KIT / "README.md")],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    after = time.time()
    start, end = week_window(printed)
    assert start.timestamp() <= after, printed
    assert before < end.timestamp(), printed
    assert start.tzinfo == UTC
    assert start >= DEMO_ANCHOR
