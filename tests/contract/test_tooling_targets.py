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
        ("us-isof-south-1", OTHER_PARTITION_MESSAGE.format(region="us-isof-south-1")),
    ],
    ids=["empty", "whitespace", "upper-case", "not-a-code", "other-partition", "iso-f"],
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


INJECTED_REGIONS = [
    "x'; touch {canary}; echo '",
    "us-west-2'; touch {canary}; echo 'us-west-2",
    "x'b",
    "$(shell touch {canary})",
    "us-west-2$(shell touch {canary})",
]


@needs_make
@pytest.mark.parametrize("target", ["bootstrap", "synth"])
@pytest.mark.parametrize("passed", ["argument", "shell-argument", "environment"])
@pytest.mark.parametrize(
    "template",
    INJECTED_REGIONS,
    ids=["quote", "quote-region", "lone-quote", "shell", "region-shell"],
)
def test_a_region_with_shell_text_runs_nothing(tmp_path, target, passed, template):
    # make expands require-region under `make -n` too, so a REGION value must reach no shell
    # (and no make function that runs one) before it has passed the format check.
    canary = tmp_path / "canary"
    region = template.format(canary=canary)
    unset = {"ALERT_EMAIL", "ACCOUNT", "CDK_ARGS", "REGION", "MODEL_ID", "FALLBACK_MODEL_ID"}
    env = {k: v for k, v in os.environ.items() if k not in unset}
    fixed = ["-n", "-C", str(REPO), target, "ACCOUNT=111122223333", "ALERT_EMAIL=you@example.com"]
    if passed == "argument":
        argv = [MAKE, *fixed, f"REGION={region}"]
    elif passed == "shell-argument":
        # As the docs pass it: REGION="$REGION" on the make line.
        env["INJECTED"] = region
        argv = ["bash", "-c", 'exec "$0" "$@" REGION="$INJECTED"', MAKE, *fixed]
    else:
        env["REGION"] = region
        argv = [MAKE, *fixed]
    done = subprocess.run(argv, capture_output=True, text=True, env=env)  # noqa: S603
    assert done.returncode != 0
    assert REGION_CODE_MESSAGE.format(region=region.strip()) in done.stderr
    # The make error is the only output: no shell printed a syntax error.
    assert len(done.stderr.strip().splitlines()) == 1, done.stderr
    assert "aws-cdk" not in done.stdout
    assert not canary.exists()


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


# `make`, then any words of the same command, then a target that needs REGION.
MAKE_REGION_TARGET = re.compile(r"\bmake\b(?:\s+[^\s;&|]+)*?\s+(bootstrap|synth|deploy)(?![\w-])")
REGION_ARGUMENT = re.compile(r"(^|\s)REGION=")


def _load_markdown_blocks():
    spec = importlib.util.spec_from_file_location(
        "_awx_markdown_blocks", CONTRACT_DIR / "markdown_blocks.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_lines_without_region(text: str) -> list[str]:
    # Commands sit in fenced code blocks; prose that names a target is not a command.
    lines = text.splitlines()
    missing = []
    for number, _, _, line in _load_markdown_blocks().code_lines(text):
        if not MAKE_REGION_TARGET.search(line):
            continue
        # A command continued with a trailing backslash counts as one line.
        command, rest = line, number
        while command.rstrip().endswith("\\") and rest < len(lines):
            command += " " + lines[rest]
            rest += 1
        if not REGION_ARGUMENT.search(command):
            missing.append(f"{number}: {line.strip()}")
    return missing


@pytest.mark.parametrize(
    ("block", "caught"),
    [
        ('```\nmake deploy ACCOUNT=1 REGION="$REGION"\n```\n', False),
        ("```\nmake ACCOUNT=111122223333 deploy\n```\n", True),
        ("```\nAWS_REGION=us-west-2 make deploy ACCOUNT=1\n```\n", True),
        ("```\nmake deploy ACCOUNT=1 AWS_REGION=us-west-2\n```\n", True),
        ('```\nmake deploy ACCOUNT=1 \\\n    REGION="$REGION"\n```\n', False),
        ("```\nuv run make synth ACCOUNT=1\n```\n", True),
        ("~~~sh\nmake bootstrap ACCOUNT=1\n~~~\n", True),
        ("```\nmake synth-test\n```\n", False),
        ("```\nmake check && cdk deploy\n```\n", False),
        ("Run make deploy ACCOUNT=1 after that.\n", False),
    ],
)
def test_the_region_check_finds_the_target_anywhere_in_a_make_command(block, caught):
    assert bool(_make_lines_without_region(block)) is caught


# The CDK CLI, then any words of the same command, then a command that targets a Region.
CDK_REGION_COMMAND = re.compile(
    r"(?:\baws-cdk@\S+|(?<![\w@/-])cdk)(?:\s+[^\s;&|]+)*?\s+(synth|deploy|destroy|diff)(?![\w-])"
)
CDK_REGION_ARGUMENT = re.compile(r"(^|\s)(-c|--context)\s+region=")


def _pasted_commands(text: str):
    """(line number, command) for each line of a pasted block, joining backslash continuations."""
    pasted = [
        (number, line)
        for number, tag, _, line in _load_markdown_blocks().code_lines(text)
        if tag not in _load_markdown_blocks().NOT_PASTED
    ]
    joined, start = "", None
    for number, line in pasted:
        joined = f"{joined} {line}" if start is not None else line
        start = number if start is None else start
        if not line.rstrip().endswith("\\"):
            yield start, joined
            joined, start = "", None
    if start is not None:
        yield start, joined


def _cdk_lines_without_region(text: str) -> list[str]:
    return [
        f"{number}: {command.strip()}"
        for number, command in _pasted_commands(text)
        if CDK_REGION_COMMAND.search(command) and not CDK_REGION_ARGUMENT.search(command)
    ]


def _bracketed_words(text: str) -> list[str]:
    # A usage synopsis's `[--flag]` is a glob to zsh ("bad pattern") and a literal argument to
    # bash. The test command `[` and `[[` are allowed.
    return [
        f"{number}: {command.strip()}"
        for number, command in _pasted_commands(text)
        if any(w.startswith("[") and w not in ("[", "[[") for w in command.split())
    ]


@pytest.mark.parametrize(
    ("block", "caught"),
    [
        ('```\nnpx -y aws-cdk@2.1144.0 synth --all -c region="$REGION"\n```\n', False),
        ("```\nnpx -y aws-cdk@2.1144.0 synth --all -c account=1\n```\n", True),
        ("```\nnpx -y aws-cdk@2.1144.0 deploy --all \\\n    -c account=1\n```\n", True),
        ('```\nnpx -y aws-cdk@2.1144.0 deploy --all \\\n    -c region="$REGION"\n```\n', False),
        ("```\nnpx -y aws-cdk@2.1144.0 destroy --exclusively AwxKitK1\n```\n", True),
        ("```\ncd infra && npx -y aws-cdk@2.1144.0 diff\n```\n", True),
        ("```\ncdk deploy AwxAgentStack --exclusively\n```\n", True),
        ("~~~sh\nnpx -y aws-cdk@2.1144.0 synth\n~~~\n", True),
        ('```\nnpx -y aws-cdk@2.1144.0 synth --context region="$REGION"\n```\n', False),
        ("```\nnpx -y aws-cdk@2.1144.0 bootstrap aws://1/us-west-2\n```\n", False),
        ("```text\nnpx -y aws-cdk@2.1144.0 synth\n```\n", False),
        ("Run npx -y aws-cdk@2.1144.0 synth after that.\n", False),
    ],
)
def test_the_cdk_region_check(block, caught):
    assert bool(_cdk_lines_without_region(block)) is caught


@pytest.mark.parametrize(
    ("block", "caught"),
    [
        ("```\nmake deploy ACCOUNT=1 [REGION=us-west-2]\n```\n", True),
        ("```\ncdk deploy --all \\\n    -c account=1 [--require-approval never]\n```\n", True),
        ('```\n[ -n "$REGION" ] || echo unset\n```\n', False),
        ('```\n[[ -n "$REGION" ]] || echo unset\n```\n', False),
        ('```\naws --query "Stacks[0].Outputs"\n```\n', False),
        ("```text\nawx-deploy [--profile P] run <kit>\n```\n", False),
    ],
)
def test_the_bracketed_word_check(block, caught):
    assert bool(_bracketed_words(block)) is caught


def _tracked_markdown() -> list[str]:
    return _load_markdown_blocks().markdown_files()


def test_every_pasted_cdk_synth_deploy_destroy_and_diff_passes_the_region():
    # With no -c region the app targets the default Region, so a pasted destroy can remove the
    # wrong deployment.
    missing = [
        f"{name}:{hit}"
        for name in _tracked_markdown()
        for hit in _cdk_lines_without_region((REPO / name).read_text())
    ]
    assert not missing, "CDK commands without -c region=:\n" + "\n".join(missing)


def test_no_pasted_block_holds_a_bracketed_optional_word():
    found = [
        f"{name}:{hit}"
        for name in _tracked_markdown()
        for hit in _bracketed_words((REPO / name).read_text())
    ]
    assert not found, "bracketed words in pasted blocks:\n" + "\n".join(found)


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
    missing = [
        f"{name}:{hit}"
        for name in tracked
        for hit in _make_lines_without_region((REPO / name).read_text())
    ]
    assert not missing, "make lines without REGION=:\n" + "\n".join(missing)
