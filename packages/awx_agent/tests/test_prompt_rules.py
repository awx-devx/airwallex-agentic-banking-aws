"""The system prompt's rules on reply language and on facts the tools did not return.

These pin wording that live runs showed the model needs. On 13 Oct 2026, live runs on Claude
Sonnet 5 replied in the language of the data to English prompts when the prompt said only "Use
the user's language". They stated a due date no tool returned, called a payment due today "not
due today" by guessing today's date, predicted a proposal's status and offered a check no tool
of its kit does. When they offered to propose an action, they said someone else must approve
it, even for an action at the AUTO risk tier.
"""

import pytest
from awx_agent.runtime.prompt import system_prompt
from awx_common.kits import KITS

LANGUAGE = (
    "Be brief and exact. Reply in the language of the user's latest message, even when tool "
    "results, documents or names are in another language. Keep IDs, codes and status names "
    "exactly as the tool returned them."
)
FACTS = (
    "- Only state an ID, intent_hash, amount or date when a tool returned it in this "
    "conversation. You may repeat a value the user gave you, and say it came from them. "
    "Otherwise, say you do not know.",
    "- Only say whether something is due, overdue or not yet due when a tool returned that "
    "status. Never work out today's date or compare dates yourself. If a tool returned the date "
    "but not the status, give the date and say you cannot tell whether it is due today.",
    "- Do not predict the risk tier or status a proposal will get, or whether it needs approval, "
    "including for an action you offer. Code sets them when you propose.",
    '- For a proposal\'s current status, follow the status rule under "After you propose".',
    "- Offer to check something yourself only when one of your read_* tools returns it. If none "
    "of them does, tell the user you cannot check it yourself.",
    "- If you could not call the tools, say plainly that you could not reach the tools, and "
    "invent nothing.",
)


def _flat(kit: str) -> str:
    # Compared with line wrapping folded to single spaces.
    return " ".join(system_prompt(kit).split())


@pytest.mark.parametrize("kit", sorted(KITS))
def test_every_kit_prompt_ends_with_the_language_rule(kit):
    assert _flat(kit).endswith(LANGUAGE)
    assert "Use the user's language" not in _flat(kit)


@pytest.mark.parametrize("kit", sorted(KITS))
def test_the_facts_section_holds_the_fact_rules_before_after_you_propose(kit):
    prompt = system_prompt(kit)
    facts = " ".join(prompt.split("\nFacts\n", 1)[1].split("\nAfter you propose\n", 1)[0].split())
    assert facts == " ".join(FACTS)
    after = prompt.split("\nAfter you propose\n", 1)[1]
    # Each fact rule appears once, in the Facts section only.
    flat_after = " ".join(after.split())
    for rule in FACTS:
        assert rule[2:] not in flat_after
    assert "I could not reach the tools" not in prompt


TIER = (
    "Code sets each proposal's risk tier when you propose. The tier decides whether a different "
    "person must approve it before anything happens, or whether it starts at once."
)


@pytest.mark.parametrize("kit", sorted(KITS))
def test_the_prompt_leaves_approval_to_the_risk_tier(kit):
    flat = _flat(kit)
    assert TIER in flat
    assert "SUBMITTED_NO_APPROVAL_NEEDED starts its run at once without approval" in flat
    # A general sentence that most proposals wait for approval reads as a default for every
    # proposal, including an action code runs at the AUTO tier.
    assert "Most proposals wait" not in flat
    assert "nothing moves until" not in flat
