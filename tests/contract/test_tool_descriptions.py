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

# Wording that states a proposal's approval outcome before code has set its tier. A read_*
# description may name the tier it returns ("the approval tier"), so this is the read rule.
APPROVAL_OUTCOME = re.compile(
    r"\bneeds?\b[^.;]*\bapprov"
    r"|\brequires?\b[^.;]*\bapprov"
    r"|\bapproval (?:is )?required\b"
    r"|\bwithout (?:approval|an? approv)"
    r"|\bno approval\b"
    r"|\bapproval[- ](?:gated|needed)\b"
    r"|\bmust (?:approve|agree)\b"
    r"|\bapproves? (?:it|them|when)\b"
    r"|\b(?:one|two|a single) approv"
    r"|\b(?:auto|dual|single)[- ]approv"
    r"|\b(?:an?|the) approver"
    r"|\b(?:once|when) approved\b"
    r"|\bafter (?:approval|it is approved)\b"
    r"|\bthe approval\b(?!\s+tier)"
    r"|\bnothing (?:moves|happens) until\b"
    r"|\bask a person\b"
    r"|\bneeds? (?:a person|someone)\b"
    r"|\b(?:a person|someone) to (?:review|approve|sign)\b"
    r"|\bfor (?:a person to|review)\b"
    r"|\bsign(?:s|ed)? (?:it |them )?off\b"
    r"|\bsign-off\b"
    r"|\b(?:runs?|starts?) (?:at once|immediately|automatically)\b",
    re.IGNORECASE,
)

# A propose_* description says what the proposal does and leaves every approval word to code,
# so outside PROPOSE_SUFFIX it may not mention approval, a sign-off, a person or a timing.
PROPOSE_FORBIDDEN = re.compile(
    r"approv"
    r"|\bsign-off\b"
    r"|\bsign it off\b"
    r"|\bmust agree\b"
    r"|\bat once\b"
    r"|\bimmediately\b"
    r"|\bautomatic"
    r"|\bneeds? a person\b"
    r"|\bfor (?:a person|review)\b"
    r"|\bsigns? off\b"
    r"|\bsign off\b"
    r"|\bhuman\b"
    r"|\breview(?:s|ed|er)?\b"
    r"|\bconfirm"
    r"|\bauthori[sz]"
    r"|\b(?:second|another|different) person\b"
    r"|\bsomeone else\b"
    r"|\bright away\b"
    r"|\bstraight away\b"
    r"|\buntil\b",
    re.IGNORECASE,
)

# "once <event>" states as a past event what code checks ("once that week has ended"). The
# model then reports the event as having happened. "each once", "runs once" and "challenged
# once" carry no event clause.
EVENT_CLAUSE = re.compile(
    r"\bonce\b(?:\s+[\w']+){0,3}?\s+(?:has|have|had|been|\w+ed)\b",
    re.IGNORECASE,
)


def _definitions():
    for kit, path in sorted(KIT_DIRS.items()):
        module = f"awx_{path.rsplit('/', 1)[-1]}.tools"
        for spec in load_kit_tools(kit, module).tools:
            yield kit, spec, tool_definition(spec)


def _schema_descriptions(schema, where="inputSchema"):
    """Every `description` in a JSON schema, with the path to it."""
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key == "description" and isinstance(value, str):
                yield where, value
            else:
                yield from _schema_descriptions(value, f"{where}.{key}")
    elif isinstance(schema, list):
        for index, value in enumerate(schema):
            yield from _schema_descriptions(value, f"{where}[{index}]")


def _descriptions(definition):
    """The tool's description, then every description in its input schema."""
    yield "description", definition["description"]
    yield from _schema_descriptions(definition["inputSchema"])


DEFINITIONS = list(_definitions())
IDS = [f"{kit}-{spec.name}" for kit, spec, _ in DEFINITIONS]
PROPOSALS = [d for d in DEFINITIONS if d[1].kind is ToolKind.PROPOSE]
PROPOSAL_IDS = [f"{kit}-{spec.name}" for kit, spec, _ in PROPOSALS]


@pytest.mark.parametrize(("kit", "spec", "definition"), DEFINITIONS, ids=IDS)
def test_no_tool_description_states_an_approval_outcome(kit, spec, definition):
    found = [
        (where, text) for where, text in _descriptions(definition) if APPROVAL_OUTCOME.search(text)
    ]
    assert not found, f"{kit} {spec.name}: {found}"


@pytest.mark.parametrize(("kit", "spec", "definition"), PROPOSALS, ids=PROPOSAL_IDS)
def test_no_propose_description_mentions_approval(kit, spec, definition):
    found = []
    for where, text in _descriptions(definition):
        if where == "description":
            text = text.removesuffix(PROPOSE_SUFFIX)
        if PROPOSE_FORBIDDEN.search(text):
            found.append((where, text))
    assert not found, f"{kit} {spec.name}: {found}"


@pytest.mark.parametrize(("kit", "spec", "definition"), PROPOSALS, ids=PROPOSAL_IDS)
def test_no_propose_description_states_a_check_as_an_event(kit, spec, definition):
    found = [
        (where, text) for where, text in _descriptions(definition) if EVENT_CLAUSE.search(text)
    ]
    assert not found, f"{kit} {spec.name}: {found}"


def test_no_read_tool_returns_a_proposal_status():
    """The status messages and the prompt tell the model that no tool returns a proposal's
    status. A read tool that takes a proposal_id would make that false: compute the sentence
    per kit in the handler instead."""
    from awx_agent.handlers.tools import STATUS_POINTER

    assert STATUS_POINTER.startswith("No tool returns a proposal's status.")
    reads = [d for d in DEFINITIONS if d[1].kind is ToolKind.READ]
    assert reads
    for kit, spec, definition in reads:
        schema = str(definition["inputSchema"])
        assert "proposal_id" not in schema, f"{kit} {spec.name}"
        assert "proposal_id" not in definition["description"], f"{kit} {spec.name}"
        assert "proposal's status" not in definition["description"], f"{kit} {spec.name}"


@pytest.mark.parametrize(("kit", "spec", "definition"), PROPOSALS, ids=PROPOSAL_IDS)
def test_every_propose_description_leaves_the_tier_to_code(kit, spec, definition):
    assert definition["description"].endswith(PROPOSE_SUFFIX)
    assert definition["description"].count("risk tier") == 1


@pytest.mark.parametrize(
    ("kit", "name"),
    [
        ("k2", "propose_freeze_card"),
        ("k2", "propose_unfreeze_card"),
        ("k2", "propose_close_card"),
        ("k6", "propose_lock_rates"),
    ],
)
def test_a_card_or_rate_action_description_opens_with_propose(kit, name):
    (definition,) = [d for k, spec, d in DEFINITIONS if (k, spec.name) == (kit, name)]
    assert definition["description"].startswith("Propose ")


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
        ("Needs a person.", True),
        ("It needs a person to sign it off.", True),
        ("Goes to an approver.", True),
        ("A short summary for the approver.", True),
        ("An approver signs it.", True),
        ("Once approved, the card is issued.", True),
        ("After approval it runs.", True),
        ("Approval is required.", True),
        ("Approval required.", True),
        ("Two people must agree.", True),
        ("Small changes are auto-approved.", True),
        ("Runs immediately.", True),
        ("Starts at once.", True),
        ("Large payments take dual approval.", True),
        ("A single-approver action.", True),
        ("A short narrative for a person to review.", True),
        ("Sent for review.", True),
        ("It is recorded in the proposal so the approval names one document.", True),
        ("The plan, with the approval tier the fundable set would need.", False),
        ("Hand a dispute to a person with a summary.", False),
        ("The needs_person flag marks items left for a person.", False),
        ("Record an operator's decision on a claim.", False),
        ("Its status, for example AWAITING_APPROVAL.", False),
        ("It is recorded in the proposal, which names one document.", False),
        (PROPOSE_SUFFIX.strip(), False),
    ],
)
def test_the_approval_outcome_check(text, caught):
    assert bool(APPROVAL_OUTCOME.search(text)) is caught


@pytest.mark.parametrize(
    ("text", "caught"),
    [
        ("Propose a card. Approval is the risk tier's call.", True),
        ("Propose it; a sign-off follows.", True),
        ("Propose it for a person to sign it off.", True),
        ("Propose it; two people must agree.", True),
        ("Propose it. It starts at once.", True),
        ("Propose it. It runs immediately.", True),
        ("Propose it. Small changes are automatic.", True),
        ("Propose it. Needs a person.", True),
        ("Propose a narrative for review.", True),
        ("Propose it. A person signs off on it.", True),
        ("Propose it. Needs sign off.", True),
        ("Propose it. It needs a human.", True),
        ("Propose it. Needs a second person.", True),
        ("Propose it. A different person must confirm it.", True),
        ("Propose it. A reviewer checks it.", True),
        ("Propose it. It is reviewed before it runs.", True),
        ("Propose it. Runs right away.", True),
        ("Propose it. Runs straight away.", True),
        ("Propose it. A person authorises it.", True),
        ("Propose it. Someone else checks it.", True),
        ("Propose it. Nothing runs until someone confirms it.", True),
        ("Propose clearing a response. Code blocks it unless a fresh read-back is empty.", False),
        ("Record an operator's decision on a claim.", False),
        ("Hand a dispute to a person with a summary.", False),
        ("Propose challenging a dispute with cited facts and a short narrative.", False),
        ("Propose collecting the customer's open bridge advance back.", False),
        ("It is recorded in the proposal, which names one document.", False),
        ("Leave unset. The server fills this with the signed-in caller.", False),
    ],
)
def test_the_propose_check(text, caught):
    assert bool(PROPOSE_FORBIDDEN.search(text)) is caught


@pytest.mark.parametrize(
    ("text", "caught"),
    [
        ("Collect a repayment for its next ISO week, once that week has ended.", True),
        ("Clear it once Airwallex has shown no such response.", True),
        ("Collect it back once the deposit landed.", True),
        ("Issue it once the run has succeeded.", True),
        ("Close it once it has been replaced.", True),
        ("Weeks run in order from the first full week after funding, each once.", False),
        ("The check runs once per proposal.", False),
        ("A dispute can be challenged once.", False),
        ("Code blocks it unless that week has ended.", False),
    ],
)
def test_the_event_clause_check(text, caught):
    assert bool(EVENT_CLAUSE.search(text)) is caught
