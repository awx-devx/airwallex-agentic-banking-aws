"""The system prompt's rules on reply language and on facts the tools did not return.

These pin wording that live runs showed the model needs. On 13 Oct 2026, live runs on Claude
Sonnet 5 replied in the language of the data to English prompts when the prompt said only "Use
the user's language". They stated a due date no tool returned, called a payment due today "not
due today" by guessing today's date, predicted a proposal's status and offered a check no tool
of its kit does. They assumed a deposit had landed and rounded amounts. When they offered to
propose an action, they said someone else must approve it, even for an action at the AUTO risk
tier. After a propose, kit 5 offered to check the status "with a read tool" next to the
`awxpoc proposals show` pointer, paraphrased the AUTO status message, decided from the balances
that code would not block a proposal, and guessed why a value was null.
"""

import pytest
from awx_agent.runtime.prompt import system_prompt
from awx_common.kits import KITS

LANGUAGE = (
    "Be brief and exact. Reply in the language of the user's latest message, even when tool "
    "results, documents or names are in another language. Keep IDs, codes, status names and "
    "amounts exactly as the tool returned them, and never round or abbreviate an amount."
)
FACTS = (
    "- Only state an ID, intent_hash, amount or date when a tool returned it in this "
    "conversation. You may repeat a value the user gave you, and say it came from them. "
    "Otherwise, say you do not know.",
    "- Only say whether something is due, overdue or not yet due when a tool returned that "
    "status. Never work out today's date or compare dates yourself. If a tool returned the date "
    "but not the status, give the date and say you cannot tell whether it is due today.",
    "- Do not predict the risk tier or status a proposal will get, or whether it needs approval, "
    "including for an action you offer. Code sets them when you propose. Do not predict whether "
    "code will block an action, and do not apply a tool description's checks to the figures "
    "yourself.",
    "- Do not say or assume that an event has happened, such as a deposit landing or a payment "
    "settling, unless a tool returned it.",
    "- Do not explain why a value is what it is unless a tool returned the reason, and do not "
    "guess one.",
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
    "person must approve it before anything happens. A proposal whose status is "
    "SUBMITTED_NO_APPROVAL_NEEDED starts its run at once without approval"
)


@pytest.mark.parametrize("kit", sorted(KITS))
def test_the_prompt_leaves_approval_to_the_risk_tier(kit):
    flat = _flat(kit)
    assert TIER in flat
    assert flat.count("starts at once") == 0
    # A general sentence that most proposals wait for approval reads as a default for every
    # proposal, including an action code runs at the AUTO tier.
    assert "Most proposals wait" not in flat
    assert "nothing moves until" not in flat


STATUS_RULE = (
    "- No tool returns a proposal's current status, so never offer to check one. Say you have "
    "not checked it and give the user `awxpoc proposals show <proposal_id>`."
)


@pytest.mark.parametrize("kit", sorted(KITS))
def test_after_a_propose_the_prompt_quotes_the_status_and_offers_no_status_check(kit):
    after = " ".join(system_prompt(kit).split("\nAfter you propose\n", 1)[1].split())
    assert (
        "Then quote the tool's status_message to the user word for word, in the user's language."
        in after
    )
    assert "using the tool's status_message" not in after
    assert STATUS_RULE in after
    # No read tool returns a proposal's status, so the rule has no read-tool branch.
    assert "read it with a read_* tool" not in after
    assert "If no read tool returns the status" not in after
