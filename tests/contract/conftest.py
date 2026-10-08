"""Keep the live sandbox out of a plain `pytest` run, and say when a sandbox run skipped it.

Tests here carry the `sandbox` marker and call the real Airwallex sandbox. The root pyproject
has no marker filter, so `uv run pytest tests/contract` would hit the sandbox. They run only when
the marker expression asks for them (`make sandbox-test` passes `-m sandbox`) or AWX_SANDBOX=1.

A sandbox run where every contract test skipped (for example because the AWX_CONTRACT_*
fixture ids are unset) still exits 0, so the terminal summary ends with a line that says so.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

import pytest

CONTRACT_DIR = Path(__file__).resolve().parent


def _sandbox_selected(config: pytest.Config) -> bool:
    expr = config.getoption("-m") or ""
    return os.environ.get("AWX_SANDBOX") == "1" or ("sandbox" in expr and "not sandbox" not in expr)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if _sandbox_selected(config):
        return
    skip = pytest.mark.skip(reason="live sandbox: run with make sandbox-test or AWX_SANDBOX=1")
    for item in items:
        if item.get_closest_marker("sandbox"):
            item.add_marker(skip)


def _is_contract_sandbox(report, rootpath: Path) -> bool:
    if "sandbox" not in getattr(report, "keywords", {}):
        return False
    path = (rootpath / report.nodeid.split("::", 1)[0]).resolve()
    return path.is_relative_to(CONTRACT_DIR)


def contract_skip_summary(stats: dict[str, Iterable], rootpath: Path) -> str | None:
    """The closing line for a sandbox run that skipped contract tests, or None.

    `stats` is the terminal reporter's outcome -> reports mapping. A test counts as run when its
    call phase passed or failed, and as skipped when any phase skipped it.
    """
    skipped = {r.nodeid for r in stats.get("skipped", ()) if _is_contract_sandbox(r, rootpath)}
    if not skipped:
        return None
    ran = {
        r.nodeid
        for outcome in ("passed", "failed")
        for r in stats.get(outcome, ())
        if getattr(r, "when", "call") == "call" and _is_contract_sandbox(r, rootpath)
    }
    hint = 'set the AWX_CONTRACT_* fixture ids (docs/testing.md, "Contract fixtures")'
    if not ran:
        return (
            f"CONTRACT SUITE SKIPPED: all {len(skipped)} contract tests skipped, so nothing "
            f"here reached the Airwallex sandbox; {hint}"
        )
    return (
        f"CONTRACT SUITE PARTLY SKIPPED: {len(skipped)} of {len(skipped) + len(ran)} contract "
        f"tests skipped; {hint}"
    )


# A wrapper, so the line comes after the short test summary (-rs) and just before the final
# counts line.
@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_terminal_summary(terminalreporter, exitstatus: int, config: pytest.Config):
    result = yield
    if _sandbox_selected(config):
        line = contract_skip_summary(terminalreporter.stats, config.rootpath)
        if line:
            terminalreporter.write_sep("=", line, yellow=True, bold=True)
    return result
