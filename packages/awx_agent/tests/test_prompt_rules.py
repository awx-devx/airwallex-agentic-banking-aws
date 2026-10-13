"""The system prompt's rules on reply language and on facts the tools did not return.

These pin wording a live run showed the model needs: Claude Sonnet 5 replied in the language of
the data to English prompts when the prompt said only "Use the user's language", and stated a
due date that no tool returned.
"""

import pytest
from awx_agent.runtime.prompt import system_prompt
from awx_common.kits import KITS

LANGUAGE = (
    "Reply in the language of the user's latest message, even when tool results, "
    "documents or names are in another language."
)
FACTS = (
    "Only state an ID, amount, date, due or overdue status, or proposal status that a tool "
    "returned in this conversation. If no tool returned it, say you do not know."
)
CHECKS = (
    "Offer to check something only with a read_* tool you have. If none returns what the user "
    "asked for, say so."
)


@pytest.mark.parametrize("kit", sorted(KITS))
def test_every_kit_prompt_carries_the_language_and_facts_rules(kit):
    # Compared with line wrapping folded to single spaces.
    prompt = " ".join(system_prompt(kit).split())
    assert LANGUAGE in prompt
    assert FACTS in prompt
    assert CHECKS in prompt
    assert "Use the user's language" not in prompt
