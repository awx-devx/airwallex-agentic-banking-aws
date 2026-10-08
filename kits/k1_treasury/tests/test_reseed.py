"""The example re-seed file: a paid obligation stays paid, an unpaid one becomes payable.

On 8 Oct 2026, on a deployment from a fresh clone into a new account, a re-seed with
"replace": true moved the dates, but the mapped obligations were already PAID, so the agent
had nothing to execute. A second re-seed that also mapped ob-office-usd to the US LOCAL
shipping beneficiary let execute_plan pay it. These tests run deploy/k1.yaml's own seed args and
kits/k1_treasury/examples/reseed_treasury_book.yaml through the shipped seed command.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml
from awx_agent.testing import KitToolHarness
from awx_common import TenantContext
from awx_common.operator_commands import OperatorContext, run_operator_command
from awx_common.sandbox import make_transfer_advance
from awx_k1_treasury import OPERATOR_COMMANDS
from awx_k1_treasury.book import load_book
from awx_k1_treasury.locks import LockState, read_lock
from awx_k1_treasury.workflow import build_specs
from awx_spine.fake import EPOCH
from awx_spine.store import InMemoryOperationStore
from awx_spine_aws.deploy import placeholders, substitute
from awx_spine_aws.local_runner import run_kit_workflow

REPO = Path(__file__).resolve().parents[3]
RESEED = REPO / "kits" / "k1_treasury" / "examples" / "reseed_treasury_book.yaml"


def _deploy_seed_args() -> dict[str, Any]:
    doc = yaml.safe_load((REPO / "deploy" / "k1.yaml").read_text())
    (step,) = [s for s in doc["steps"] if s["command"] == "seed_treasury_book"]
    return step["args"]


def test_reseed_file_is_the_deploy_seed_with_replace_and_an_unpaid_obligation():
    reseed = yaml.safe_load(RESEED.read_text())
    deploy = _deploy_seed_args()
    assert reseed.pop("replace") is True
    added = reseed["beneficiaries"].pop("ob-office-usd")
    assert added == "${K1_BEN_OFFICE_USD}"
    assert reseed["beneficiaries"] == deploy["beneficiaries"]
    assert reseed["book_version"] != deploy["book_version"]


def _execute(fake, h, specs, ids):
    return run_kit_workflow(
        specs["execute_plan"],
        {"obligation_ids": ids},
        proposed_by="requester",
        tenant=TenantContext("platform"),
        port=fake,
        store=InMemoryOperationStore(),
        decide=lambda p: [],
        now=lambda: EPOCH + timedelta(seconds=fake.clock()),
        sleep=fake.advance,
        kit_store=h.store(),
        platform_store=h.store(),
        actions=tuple(specs),
    )


def test_a_reseed_keeps_a_paid_obligation_paid_and_pays_the_newly_mapped_one(env):
    fake = env.fake
    now = EPOCH + timedelta(seconds=fake.clock())
    h = KitToolHarness.for_kit("k1", port=fake, clock=lambda: now)
    ctx = OperatorContext(
        kit="k1", platform_store=h.store(), store_for=h.store, now=now, read=fake.read
    )
    shipping = env.book.obligation("ob-shipping").beneficiary_id
    variables = {
        "K1_BEN_SUPPLIER_EUR": env.book.obligation("ob-supplier-eur").beneficiary_id,
        "K1_BEN_SHIPPING": shipping,
        "K1_BEN_OFFICE_USD": shipping,  # the shape the 8 Oct re-seed used
    }
    first = substitute(_deploy_seed_args(), variables)
    assert placeholders(first) == []
    run_operator_command(OPERATOR_COMMANDS, "seed_treasury_book", ctx, first)
    specs = build_specs(transfer_advance=make_transfer_advance("k1"))
    paid = _execute(fake, h, specs, ["ob-shipping"])
    assert paid.state == "SUCCEEDED", paid.detail

    reseed = substitute(yaml.safe_load(RESEED.read_text()), variables)
    assert placeholders(reseed) == []
    out = run_operator_command(OPERATOR_COMMANDS, "seed_treasury_book", ctx, reseed)
    assert out["replaced"] is True
    assert {o.obligation_id for o in load_book(h.store()).obligations} == {
        "ob-shipping",
        "ob-supplier-eur",
        "ob-office-usd",
    }
    # The re-seed moved the dates only: the paid obligation is still PAID and cannot pay again.
    assert read_lock(h.store(), "ob-shipping").state is LockState.PAID
    again = _execute(fake, h, specs, ["ob-shipping"])
    assert again.state == "BLOCKED", again
    # The newly mapped obligation pays, with no approver.
    office = _execute(fake, h, specs, ["ob-office-usd"])
    assert office.state == "SUCCEEDED", office.detail
    assert office.proposal.risk_tier.value == "AUTO"
    assert read_lock(h.store(), "ob-office-usd").state is LockState.PAID
    assert [t["status"] for t in fake.objects("transfers")] == ["PAID", "PAID"]
