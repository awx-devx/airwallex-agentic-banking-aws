"""Offline checks on the Makefile targets and the sandbox run's skip summary.

These pin the deploy tooling a participant follows from docs/deployment.md: `make bootstrap`
must not load the CDK app (which needs an alert email), `CDK_ARGS` must reach `cdk synth` and
`cdk deploy`, and a sandbox run that skipped the contract suite must say so in its summary.
Nothing here calls AWS or the Airwallex sandbox.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[2]
CONTRACT_DIR = REPO / "tests" / "contract"

MAKE = shutil.which("make")
needs_make = pytest.mark.skipif(MAKE is None, reason="make is not installed")


def make_dry_run(*args: str) -> str:
    env = {k: v for k, v in os.environ.items() if k not in {"ALERT_EMAIL", "ACCOUNT", "CDK_ARGS"}}
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [MAKE, "-n", "-C", str(REPO), *args],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return done.stdout


@needs_make
def test_bootstrap_needs_only_the_account_and_does_not_load_the_app():
    out = make_dry_run("bootstrap", "ACCOUNT=111122223333")
    cdk_lines = [line for line in out.splitlines() if "aws-cdk" in line]
    assert len(cdk_lines) == 1
    line = cdk_lines[0]
    assert "bootstrap aws://111122223333/ap-southeast-2" in line
    # Run from the repository root, where no cdk.json names the app.
    assert "cd infra" not in line
    assert "alert_email" not in line
    assert not (REPO / "cdk.json").exists()


@needs_make
def test_bootstrap_without_an_account_is_refused():
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [MAKE, "-C", str(REPO), "bootstrap"],
        capture_output=True,
        text=True,
        env={k: v for k, v in os.environ.items() if k != "ACCOUNT"},
    )
    assert done.returncode != 0
    assert "set ACCOUNT" in done.stderr


@needs_make
@pytest.mark.parametrize("target", ["synth", "deploy"])
def test_cdk_args_reach_synth_and_deploy(target):
    out = make_dry_run(
        target,
        "ACCOUNT=111122223333",
        "ALERT_EMAIL=you@example.com",
        "CDK_ARGS=--require-approval never",
    )
    line = next(line for line in out.splitlines() if "aws-cdk" in line)
    assert f"aws-cdk@2.1144.0 {target} --all" in line
    assert line.rstrip().endswith("--require-approval never")


@needs_make
def test_sandbox_test_prints_skip_reasons():
    out = make_dry_run("sandbox-test")
    line = next(line for line in out.splitlines() if "pytest" in line)
    assert "-m sandbox" in line
    assert "-rs" in line.split()


def _load_contract_conftest():
    spec = importlib.util.spec_from_file_location(
        "_awx_contract_conftest", CONTRACT_DIR / "conftest.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _report(nodeid: str, when: str = "call", sandbox: bool = True):
    keywords = {"sandbox": 1} if sandbox else {}
    return SimpleNamespace(nodeid=nodeid, when=when, keywords=keywords)


CONTRACT = "tests/contract/test_sandbox_contract.py::test_"


def test_summary_says_the_whole_contract_suite_skipped():
    summary = _load_contract_conftest().contract_skip_summary
    stats = {
        "skipped": [_report(f"{CONTRACT}a", "setup"), _report(f"{CONTRACT}b", "setup")],
        "passed": [_report("kits/k1_treasury/tests/test_acceptance.py::test_x")],
    }
    line = summary(stats, REPO)
    assert line.startswith("CONTRACT SUITE SKIPPED: all 2 contract tests skipped")
    assert "AWX_CONTRACT_" in line


def test_summary_counts_a_partly_skipped_contract_suite():
    summary = _load_contract_conftest().contract_skip_summary
    stats = {
        "skipped": [_report(f"{CONTRACT}a", "setup")],
        "passed": [_report(f"{CONTRACT}b", "setup"), _report(f"{CONTRACT}b")],
    }
    assert summary(stats, REPO).startswith("CONTRACT SUITE PARTLY SKIPPED: 1 of 2")


def test_summary_is_silent_when_no_contract_test_skipped():
    summary = _load_contract_conftest().contract_skip_summary
    stats = {
        "passed": [_report(f"{CONTRACT}a")],
        "skipped": [_report("kits/k4_dispute/tests/test_live.py::test_x", "setup")],
    }
    assert summary(stats, REPO) is None


def test_a_sandbox_run_without_fixture_ids_ends_with_the_skip_line():
    # The contract module skips at collection when any AWX_CONTRACT_* id is unset, so this run
    # sends nothing to the sandbox.
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWX_CONTRACT_")}
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-m",
            "sandbox",
            "-rs",
            str(CONTRACT_DIR / "test_sandbox_contract.py"),
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    lines = [line for line in done.stdout.splitlines() if line.strip()]
    assert "passed" not in lines[-1]
    # The skip line closes the summary, just above pytest's own counts line.
    assert "CONTRACT SUITE SKIPPED: all" in lines[-2]
