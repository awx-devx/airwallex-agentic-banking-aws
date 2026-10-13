"""No tool description says in advance whether a proposal needs approval.

Code sets a proposal's risk tier when the agent proposes, and the tier decides whether a
different person must approve it. A live run on 13 Oct 2026 showed Claude Sonnet 5 repeating
a general approval sentence for an action code runs at the AUTO tier, so every description
the agent sees (the Gateway ToolDefinition) must leave that to code.
"""

from __future__ import annotations

import re

import pytest
from awx_agent.registry import load_kit_tools
from awx_agent.schema import PROPOSE_SUFFIX, tool_definition
from awx_common import ToolKind
from awx_common.kits import KIT_DIRS

# Wording that states a proposal's approval outcome before code has set its tier.
APPROVAL_OUTCOME = re.compile(
    r"\bneeds?\b[^.;]*\bapprov"
    r"|\brequires?\b[^.;]*\bapprov"
    r"|\bwithout (?:approval|an? approv)"
    r"|\bno approval\b"
    r"|\bapproval[- ](?:gated|needed)\b"
    r"|\bmust approve\b"
    r"|\bapproves? (?:it|them|when)\b"
    r"|\b(?:one|two|a single) approv"
    r"|\bnothing (?:moves|happens) until\b"
    r"|\bask a person\b"
    r"|\bruns? (?:at once|automatically)\b",
    re.IGNORECASE,
)


def _definitions():
    for kit, path in sorted(KIT_DIRS.items()):
        module = f"awx_{path.rsplit('/', 1)[-1]}.tools"
        for spec in load_kit_tools(kit, module).tools:
            yield kit, spec, tool_definition(spec)


DEFINITIONS = list(_definitions())


@pytest.mark.parametrize(
    ("kit", "spec", "definition"),
    DEFINITIONS,
    ids=[f"{kit}-{spec.name}" for kit, spec, _ in DEFINITIONS],
)
def test_no_tool_description_states_an_approval_outcome(kit, spec, definition):
    found = APPROVAL_OUTCOME.findall(definition["description"])
    assert not found, f"{kit} {spec.name}: {definition['description']}"


@pytest.mark.parametrize(
    ("kit", "spec", "definition"),
    [d for d in DEFINITIONS if d[1].kind is ToolKind.PROPOSE],
    ids=[f"{kit}-{spec.name}" for kit, spec, _ in DEFINITIONS if spec.kind is ToolKind.PROPOSE],
)
def test_every_propose_description_leaves_the_tier_to_code(kit, spec, definition):
    assert definition["description"].endswith(PROPOSE_SUFFIX)
    assert definition["description"].count("risk tier") == 1


@pytest.mark.parametrize(
    ("text", "caught"),
    [
        ("Creates a proposal only; nothing moves until a different person approves it.", True),
        ("Freeze a card now (revokes its authority; no approval needed).", True),
        ("A raise needs approval; a pure reduction runs without one.", True),
        ("Needs one approver.", True),
        ("It moves no money and runs without approval.", True),
        ("starts one approval-gated run per employer", True),
        ("Accepting always needs a person's approval.", True),
        ("A person must approve it.", True),
        ("Ask a person to release a stuck claim.", True),
        ("Needs two approvers.", True),
        ("The plan, with the approval tier the fundable set would need.", False),
        ("Hand a dispute to a person with a summary.", False),
        ("Creates a proposal only. Code sets its risk tier when you propose.", False),
    ],
)
def test_the_approval_outcome_check(text, caught):
    assert bool(APPROVAL_OUTCOME.search(text)) is caught
