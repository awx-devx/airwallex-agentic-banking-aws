"""Kit 6's required acceptance scenario, sandbox-marked (`make sandbox-test`).

This test runs it on FakeAirwallex. test_live_sandbox.py runs the same scenario and amounts
through AirwallexClient on the shared sandbox, holding `flock /tmp/awx-sandbox.lock` for the
whole run, with three existing connected accounts found by their `poc-i6-` label (they cannot
be deleted) and a fresh book per run (M2). Afterwards: nothing left at SENT, no pending
authorisation (kit 6 issues no cards).
"""

from decimal import Decimal

import pytest
from awx_common import Money
from awx_k6_payroll import PAYROLL_SPEC
from awx_k6_payroll.books import read_book
from awx_k6_payroll.workflow import LOCK_SPEC
from k6_data import PERIOD, batch
from k6_fake import children, make_env, run

pytestmark = pytest.mark.sandbox


def _need_b() -> Money:
    env = make_env(b="1000000")
    raw = {"pay_period": PERIOD, "employers": [batch()["employers"][1]]}
    run(LOCK_SPEC, raw, env)
    child = run(PAYROLL_SPEC, raw, env, approvers=()).proposal.computed["children"][0]
    sells = sum((v["sell"] for v in child["fx_lock"].values()), Money.zero("USD"))
    usd = child["pricing"]["by_currency"]["USD"]["need"]
    return sells + usd + child["pricing"]["platform_fee"]


def test_kit6_acceptance_fund_a_short_b_deposit_c():
    short = str((_need_b() - Money.of("0.01", "USD")).amount)
    env = make_env(a="20000", b=short, c="5000")
    ca = env.accts["c"]
    env.fake.after_write["transfers.create"] = lambda f, op: (
        op.on_behalf_of == env.accts["a"] and not f.txns and f.deposit(ca, "1000.00")
    )
    run(LOCK_SPEC, batch(), env)
    out = run(PAYROLL_SPEC, batch(), env)
    kids = children(out)
    assert out.state == "SUCCEEDED", out.detail
    assert [kids[k].state for k in "abc"] == ["SUCCEEDED", "BLOCKED", "SUCCEEDED"]
    assert env.money_ops(acct=env.accts["b"]) == []
    assert env.fake.balance("USD", env.accts["b"]) == Money.of(short, "USD")
    assert env.fake.txns[0]["amount"] == Decimal("1000.00")
    for conv in env.fake.objects("conversions"):
        assert conv["request_id"] in {op.request_id for op in env.money_ops(acct=env.accts["a"])}
    assert env.fake.balance("EUR").is_zero()  # the platform wallet never converted
    assert {t["status"] for t in env.fake.objects("transfers")} == {"PAID"}  # none at SENT
    assert read_book(env.platform_store) is not None
