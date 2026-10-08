"""deploy/k5.yaml's demo obligation must be due today, and the re-seed file moves it.

On 8 Oct 2026, on a deployment from a fresh clone into a new account, the demo obligation was
seeded a few days ahead and the shortfall was refused ("payment is not due today"). A re-seed
with today's date and "replace": true fixed it. These tests run the deploy file's own seed
args and the example re-seed file through the shipped operator commands and tools.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml
from awx_agent.testing import KitToolHarness
from awx_common.operator_commands import run_operator_command
from awx_k5_platform_spend import OPERATOR_COMMANDS
from awx_k5_platform_spend.workflow import configure_platform
from awx_spine_aws.deploy import placeholders, substitute
from k5_support import TODAY, Platform, obligation, operator_context

REPO = Path(__file__).resolve().parents[3]
RESEED = REPO / "kits" / "k5_platform_spend" / "examples" / "reseed_platform_setup.yaml"


def _deploy_seed_args() -> dict[str, Any]:
    doc = yaml.safe_load((REPO / "deploy" / "k5.yaml").read_text())
    (step,) = [s for s in doc["steps"] if s["command"] == "seed_platform_setup"]
    return step["args"]


def _variables(p: Platform, due: str) -> dict[str, str]:
    return {
        "K5_ACCT_ALPHA": p.acct["t-alpha"],
        "K5_ACCT_BRAVO": p.acct["t-bravo"],
        "K5_ACCT_CHARLIE": p.acct["t-charlie"],
        "K5_DUE_ON": due,
        "K5_EXPECTED_DEPOSIT_ON": due,
    }


def test_reseed_file_is_the_deploy_seed_with_replace():
    reseed = yaml.safe_load(RESEED.read_text())
    assert reseed.pop("replace") is True
    assert reseed == _deploy_seed_args()


def test_obligation_due_later_is_refused_and_the_reseed_makes_it_due_today():
    configure_platform(None)
    p = Platform(obligations=(obligation("t-alpha", "ob-a1", "500"),))
    h = KitToolHarness.for_kit("k5", port=p.fake, tenants=dict(p.acct), clock=p.now)
    ctx = operator_context(p, h.store("platform"))

    later = substitute(_deploy_seed_args(), _variables(p, str(TODAY + timedelta(days=3))))
    assert placeholders(later) == []
    run_operator_command(OPERATOR_COMMANDS, "seed_platform_setup", ctx, later)
    run_operator_command(
        OPERATOR_COMMANDS, "fund_capital", ctx, {"key": "initial", "amount": "1000"}
    )
    token = h.issuer.token("user-requester", tenant_id="t-alpha", now=p.now().timestamp())
    refused = h.call("propose_register_shortfall", {"obligation_ref": "ob-a1"}, token=token)
    assert refused["risk_tier"] == "BLOCKED", refused
    assert "payment is not due today" in " ".join(refused["reasons"]), refused
    early = h.call("propose_bridge_shortfall", {"obligation_ref": "ob-a1"}, token=token)
    assert early["risk_tier"] == "BLOCKED", early
    assert "payment is not due today" in " ".join(early["reasons"]), early

    today = substitute(yaml.safe_load(RESEED.read_text()), _variables(p, str(TODAY)))
    out = run_operator_command(OPERATOR_COMMANDS, "seed_platform_setup", ctx, today)
    assert out["replaced"] is True
    bridge = h.call("propose_bridge_shortfall", {"obligation_ref": "ob-a1"}, token=token)
    assert bridge["status"] == "AWAITING_APPROVAL", bridge
