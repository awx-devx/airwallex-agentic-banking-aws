"""Offline checks on the Makefile targets and the sandbox run's skip summary.

These pin the deploy tooling a participant follows from docs/deployment.md: `make bootstrap`
must not load the CDK app (which needs an alert email), `CDK_ARGS` must reach `cdk synth` and
`cdk deploy`, and a sandbox run that skipped the contract suite must say so in its summary.
Nothing here calls AWS or the Airwallex sandbox.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from awx_common.regions import (
    DEFAULT_REGION,
    OTHER_PARTITION_MESSAGE,
    OTHER_PARTITION_PREFIXES,
    REGION_CODE,
    REGION_CODE_MESSAGE,
)

REPO = Path(__file__).resolve().parents[2]
CONTRACT_DIR = REPO / "tests" / "contract"

MAKE = shutil.which("make")
needs_make = pytest.mark.skipif(MAKE is None, reason="make is not installed")


def make_dry_run(*args: str) -> str:
    unset = {"ALERT_EMAIL", "ACCOUNT", "CDK_ARGS", "REGION", "MODEL_ID", "FALLBACK_MODEL_ID"}
    env = {k: v for k, v in os.environ.items() if k not in unset}
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
    assert f"bootstrap aws://111122223333/{DEFAULT_REGION}" in line
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


def test_makefile_default_region_is_the_python_default():
    lines = (REPO / "Makefile").read_text().splitlines()
    (line,) = [line for line in lines if line.startswith("REGION ?=")]
    assert line.split("?=", 1)[1].strip() == DEFAULT_REGION


@needs_make
def test_bootstrap_uses_the_region_given():
    out = make_dry_run("bootstrap", "ACCOUNT=111122223333", "REGION=eu-west-1")
    line = next(line for line in out.splitlines() if "aws-cdk" in line)
    assert "bootstrap aws://111122223333/eu-west-1" in line


@needs_make
@pytest.mark.parametrize("target", ["synth", "deploy"])
def test_region_and_model_ids_reach_synth_and_deploy(target):
    base = ("ACCOUNT=111122223333", "ALERT_EMAIL=you@example.com")
    line = next(x for x in make_dry_run(target, *base).splitlines() if "aws-cdk" in x)
    assert f"-c region={DEFAULT_REGION}" in line
    assert "model_id" not in line
    out = make_dry_run(
        target, *base, "REGION=eu-west-1", "MODEL_ID=m.primary", "FALLBACK_MODEL_ID=m.fallback"
    )
    line = next(x for x in out.splitlines() if "aws-cdk" in x)
    assert "-c region=eu-west-1" in line
    assert "-c model_id=m.primary" in line
    assert "-c fallback_model_id=m.fallback" in line


def _makefile_variable(name: str) -> str:
    lines = (REPO / "Makefile").read_text().splitlines()
    (line,) = [line for line in lines if line.startswith(f"{name} ")]
    return line.split("=", 1)[1].strip()


def test_makefile_region_checks_are_the_python_ones():
    # make writes a literal $ as $$, and refers to the value as $(REGION_VALUE).
    assert _makefile_variable("REGION_CODE").replace("$$", "$") == REGION_CODE
    assert tuple(_makefile_variable("OTHER_PARTITION_PREFIXES").split()) == (
        OTHER_PARTITION_PREFIXES
    )
    for name, message in (
        ("REGION_CODE_MESSAGE", REGION_CODE_MESSAGE),
        ("OTHER_PARTITION_MESSAGE", OTHER_PARTITION_MESSAGE),
    ):
        assert _makefile_variable(name) == message.format(region="$(REGION_VALUE)")


@needs_make
@pytest.mark.parametrize("target", ["bootstrap", "synth", "deploy"])
@pytest.mark.parametrize(
    ("region", "message"),
    [
        ("", "REGION is empty. Set it first, for example: export REGION=us-west-2"),
        ("  ", "REGION is empty. Set it first, for example: export REGION=us-west-2"),
        ("US-WEST-2", REGION_CODE_MESSAGE.format(region="US-WEST-2")),
        ("zz test", REGION_CODE_MESSAGE.format(region="zz test")),
        ("us-gov-west-1", OTHER_PARTITION_MESSAGE.format(region="us-gov-west-1")),
    ],
    ids=["empty", "whitespace", "upper-case", "not-a-code", "other-partition"],
)
def test_an_empty_region_is_refused_before_cdk_runs(target, region, message):
    # `REGION=$REGION` with REGION unset in the shell passes an empty value, which overrides
    # the Makefile default. It, a value that is not a Region code and a Region in another
    # partition must stop with a message before cdk runs, even under `make -n`.
    unset = {"ALERT_EMAIL", "ACCOUNT", "CDK_ARGS", "REGION", "MODEL_ID", "FALLBACK_MODEL_ID"}
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            MAKE,
            "-n",
            "-C",
            str(REPO),
            target,
            "ACCOUNT=111122223333",
            "ALERT_EMAIL=you@example.com",
            f"REGION={region}",
        ],
        capture_output=True,
        text=True,
        env={k: v for k, v in os.environ.items() if k not in unset},
    )
    assert done.returncode != 0
    assert message in done.stderr
    assert "aws-cdk" not in done.stdout


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


MAKE_REGION_TARGET = re.compile(r"\bmake\s+(bootstrap|synth|deploy)(?![\w-])")


def test_every_documented_make_bootstrap_synth_and_deploy_passes_region():
    # A documented command (in a fenced code block) without REGION= deploys to the default
    # Region in a shell where REGION is unset, so it can create a second deployment beside the
    # one being updated.
    tracked = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "-C", str(REPO), "ls-files", "*.md"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert tracked
    missing = []
    for name in tracked:
        lines = (REPO / name).read_text().splitlines()
        in_code = False
        for number, line in enumerate(lines, 1):
            if line.lstrip().startswith("```"):
                in_code = not in_code
                continue
            # Commands sit in fenced code blocks; prose that names a target is not a command.
            if not in_code or not MAKE_REGION_TARGET.search(line):
                continue
            # A command continued with a trailing backslash counts as one line.
            command, rest = line, number
            while command.rstrip().endswith("\\") and rest < len(lines):
                command += " " + lines[rest]
                rest += 1
            if "REGION=" not in command:
                missing.append(f"{name}:{number}: {line.strip()}")
    assert not missing, "make lines without REGION=:\n" + "\n".join(missing)
