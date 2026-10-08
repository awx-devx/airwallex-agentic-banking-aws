"""Rule 0: every build may run more than once for the same proposal (a Lambda retry before its
operations are dispatched). Each build here runs twice per call, the first result thrown away,
through the real runner. One send per operation, a clean postcondition, nothing stranded."""

from __future__ import annotations

from dataclasses import replace

import pytest
from awx_spine.fake import Fault
from k5_support import Platform, obligation, usd

CARD = {"cardholder_ref": "ops-lead", "program": "standard"}


def build_twice(p: Platform, action: str) -> None:
    spec = p.specs[action]

    def twice(build):
        def run(ctx):
            build(ctx)  # thrown away: the Lambda died before returning it
            return build(ctx)

        return run

    steps = tuple(replace(s, build=twice(s.build)) for s in spec.steps)
    p.specs[action] = replace(spec, steps=steps)


def posts(p: Platform) -> list[str]:
    return [r[1] for r in p.fake.requests if r[0] == "POST"]


def test_register_shortfall_twice():
    p = Platform(obligations=(obligation("t-alpha", "ob-a1", "500"),))
    build_twice(p, "register_shortfall")
    out = p.run("register_shortfall", {"obligation_ref": "ob-a1"}, tenant="t-alpha")
    assert out.state == "SUCCEEDED", out.detail
    assert out.postcondition.ok
    assert posts(p) == []
    assert p.book.queue_entry(("t-alpha", "ob-a1"))[0].shortfall == usd("300")


def test_bridge_twice_sends_once_and_settles():
    p = Platform(obligations=(obligation("t-alpha", "ob-a1", "500"),))
    build_twice(p, "bridge_shortfall")
    out = p.run("bridge_shortfall", {"obligation_ref": "ob-a1"}, tenant="t-alpha")
    assert out.state == "SUCCEEDED", out.detail
    assert out.postcondition.ok
    assert posts(p).count("/api/v1/connected_account_transfers/create") == 1
    assert p.book.ledger.holds() == ()
    assert p.book.ledger.receivable("t-alpha") == usd("300")


def test_bridge_twice_refused_first_send_strands_no_hold():
    p = Platform(obligations=(obligation("t-alpha", "ob-a1", "500"),))
    build_twice(p, "bridge_shortfall")
    p.fake.inject("connected_account_transfers.create", Fault.INSUFFICIENT_FUNDS)
    out = p.run("bridge_shortfall", {"obligation_ref": "ob-a1"}, tenant="t-alpha")
    assert out.state == "FAILED"
    assert "hold released" in out.postcondition.detail
    assert p.book.ledger.holds() == ()


def test_reconcile_bridge_twice():
    p = Platform(obligations=(obligation("t-alpha", "ob-a1", "500"),))
    p.fake.inject(
        "connected_account_transfers.create", Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=3600
    )
    p.run("bridge_shortfall", {"obligation_ref": "ob-a1"}, tenant="t-alpha")
    p.advance(hours=1, seconds=5)
    build_twice(p, "reconcile_bridge")
    out = p.run("reconcile_bridge", {"obligation_ref": "ob-a1"}, tenant="t-alpha")
    assert out.state == "SUCCEEDED", out.detail
    assert out.postcondition.ok
    assert p.book.ledger.holds() == ()
    assert len(p.book.ledger.entries()) == 2  # capital + one advance


@pytest.mark.parametrize("refused", [False, True])
def test_fee_twice(refused):
    p = Platform()
    build_twice(p, "collect_fee")
    if refused:
        p.fake.inject("charges.create", Fault.INSUFFICIENT_FUNDS)
    out = p.run("collect_fee", {"period": "2026-10"}, tenant="t-charlie")
    assert posts(p).count("/api/v1/charges/create") == 1
    if refused:
        assert out.state == "FAILED"
        assert out.postcondition.detail.startswith("fee rejected")
        again = p.run("collect_fee", {"period": "2026-10"}, tenant="t-charlie")
        assert again.state == "SUCCEEDED"  # the next attempt is not wedged
    else:
        assert out.state == "SUCCEEDED", out.detail
        assert p.book.ledger.fee_revenue() == usd("25")


def test_repayment_twice():
    p = Platform(obligations=(obligation("t-alpha", "ob-a1", "500"),))
    assert (
        p.run("bridge_shortfall", {"obligation_ref": "ob-a1"}, tenant="t-alpha").state
        == "SUCCEEDED"
    )
    build_twice(p, "collect_repayment")
    out = p.run("collect_repayment", {}, tenant="t-alpha")
    assert out.state == "SUCCEEDED", out.detail
    assert posts(p).count("/api/v1/charges/create") == 1
    assert p.book.ledger.receivable("t-alpha") == usd("0")


def test_issue_card_twice():
    p = Platform()
    build_twice(p, "issue_card")
    out = p.run("issue_card", CARD, tenant="t-alpha")
    assert out.state == "SUCCEEDED", out.detail
    assert posts(p).count("/api/v1/issuing/cardholders/create") == 1
    assert posts(p).count("/api/v1/issuing/cards/create") == 1


def test_simulate_twice_authorises_once():
    p = Platform()
    card = p.run("issue_card", CARD, tenant="t-alpha").results["create_card"][0].provider_id
    build_twice(p, "simulate_card_spend")
    raw = {
        "scenario_ref": "s-1",
        "card_id": card,
        "amount": "5.00",
        "currency": "USD",
        "merchant_category_code": "5734",
    }
    out = p.run("simulate_card_spend", raw, tenant="t-alpha")
    assert out.state == "SUCCEEDED", out.detail
    assert out.postcondition.detail == "CLEARED"
    assert len(p.fake.objects("card_transactions")) == 1
