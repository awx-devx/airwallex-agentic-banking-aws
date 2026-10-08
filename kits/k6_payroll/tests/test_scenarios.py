"""Kit 6 scenarios on the real spine: awx_spine_aws.local_runner against FakeAirwallex."""

from decimal import Decimal

import pytest
from awx_common import ApprovalState, Money, Origin, RiskTier
from awx_k6_payroll import EMPLOYER_SPEC, PAYROLL_SPEC
from awx_k6_payroll.workflow import LOCK_SPEC
from awx_spine import fake as fake_module
from awx_spine.fake import Fault
from k6_data import PERIOD, batch, child_raw, de, employer, gb, us
from k6_fake import children, make_env, run


def one(code: str, *lines, funding: str = "USD"):
    return {"pay_period": PERIOD, "employers": [employer(code, *lines, funding=funding)]}


def pay(raw, env, **kw):
    run(LOCK_SPEC, raw, env)
    return run(PAYROLL_SPEC, raw, env, **kw)


def statuses(env):
    return sorted(t["status"] for t in env.fake.objects("transfers"))


def need_of(code: str) -> Money:
    """B's whole requirement in USD: direct payouts, locked FX sells and the fee."""
    env = make_env(**{code: "1000000"})
    raw = {
        "pay_period": PERIOD,
        "employers": [e for e in batch()["employers"] if e["employer"] == code],
    }
    run(LOCK_SPEC, raw, env)
    proposal = run(PAYROLL_SPEC, raw, env, approvers=()).proposal
    child = proposal.computed["children"][0]
    sells = sum((v["sell"] for v in child["fx_lock"].values()), Money.zero("USD"))
    usd = child["pricing"]["by_currency"].get("USD", {"need": Money.zero("USD")})["need"]
    return sells + usd + child["pricing"]["platform_fee"]


def test_required_acceptance_fund_a_short_b_deposit_c_mid_run():
    """Report section 6: fund A, leave B short of payroll plus fee, deposit into C during the
    run. A completes, B escalates without cross-funding, C's deposit never changes B's
    decision, every FX delta lands in the intended wallet, every transfer is terminal."""
    short_b = str((need_of("b") - Money.of("0.01", "USD")).amount)
    env = make_env(a="20000", b=short_b, c="5000")
    control = make_env(a="20000", b=short_b, c="5000")
    control.fake.deposit(control.accts["c"], "100000.00")  # a big deposit into C before
    b_control = children(pay(batch(), control))["b"]

    ca = env.accts["c"]
    fired: list[str] = []

    def deposits(f, op):
        if op.on_behalf_of == env.accts["a"] and "a" not in fired:
            fired.append("a")
            f.deposit(ca, "100000.00")  # during A's run, before C's run starts
        elif op.on_behalf_of == ca and "c" not in fired:
            fired.append("c")
            f.deposit(ca, "5.00")  # inside C's own run, as it pays

    env.fake.after_write["transfers.create"] = deposits
    eur_before = env.fake.price("EUR")

    def market_moves(_p):  # between approval and execution
        env.fake.set_price("EUR", eur_before * Decimal("1.05"))

    out = pay(batch(), env, on_decide=market_moves)
    assert out.state == "SUCCEEDED", out.detail
    a, b, c = (children(out)[k] for k in "abc")
    assert a.state == "SUCCEEDED", a.postcondition
    assert c.state == "SUCCEEDED", c.postcondition
    assert b.state == "BLOCKED"
    assert any("SHORTFALL 0.01 USD" in r for r in b.proposal.reasons)
    assert any("no other employer's funds" in r for r in b.proposal.reasons)
    assert b.proposal.reasons == b_control.proposal.reasons  # C's deposits changed nothing
    assert env.money_ops(acct=env.accts["b"]) == []
    assert env.fake.balance("USD", env.accts["b"]) == Money.of(short_b, "USD")
    # Both deposits into C landed (one during A's run, one during C's own run).
    assert [t["amount"] for t in env.fake.txns] == [Decimal("100000.00"), Decimal("5.00")]
    # Every conversion sold exactly the approved locked amount, in A's wallet only.
    approved = {ch["employer"]: ch.get("fx_lock", {}) for ch in out.proposal.computed["children"]}
    convs = env.fake.objects("conversions")
    assert len(convs) == 1
    assert Money.of(convs[0]["sell_amount"], "USD") == approved["a"]["EUR"]["sell"]
    assert env.fake.balance("EUR", env.accts["a"]).is_zero()
    assert env.fake.balance("EUR").is_zero()  # the platform wallet was never converted
    # Every submitted transfer reached a recorded terminal state.
    assert statuses(env) == ["PAID"] * 3


def test_the_child_intents_match_the_engines_listed_check():
    """The spine's engine refuses a child unless canonically equal to an item of a computed list."""
    env = make_env(a="20000", b="20000", c="20000")
    out = pay(batch(), env)
    assert out.state == "SUCCEEDED", out.detail
    listed = out.proposal.computed["children"]
    assert [c.proposal.intent for c in out.child_outcomes] == [
        EMPLOYER_SPEC.validate(ch) for ch in listed
    ]
    assert out.proposal.risk_tier is RiskTier.SINGLE
    assert {c.proposal.risk_tier for c in out.child_outcomes} == {RiskTier.AUTO}


def test_agent_origin_batch_pays_all_three_employers():
    env = make_env(a="20000", b="20000", c="20000")
    lock = run(LOCK_SPEC, batch(), env, origin=Origin.AGENT, proposed_by="model", approvers=())
    assert lock.state == "SUCCEEDED"  # AUTO under AGENT: no approver at all
    out = run(PAYROLL_SPEC, batch(), env, origin=Origin.AGENT, proposed_by="model")
    assert out.proposal.origin is Origin.AGENT
    assert out.proposal.risk_tier is RiskTier.SINGLE
    assert {k: c.state for k, c in children(out).items()} == dict.fromkeys("abc", "SUCCEEDED")
    assert statuses(env) == ["PAID"] * 6


def test_the_rate_lock_runs_without_approval_and_the_payroll_needs_one():
    env = make_env(a="20000")
    lock = run(LOCK_SPEC, one("a", de("anna", "100.00")), env, approvers=())
    assert lock.state == "SUCCEEDED"
    pending = run(PAYROLL_SPEC, one("a", de("anna", "100.00")), env, approvers=())
    assert pending.state == ApprovalState.EXPIRED.value
    assert env.money_ops() == []


def test_without_a_lock_an_fx_employer_is_blocked_and_the_rest_pay():
    env = make_env(a="20000", c="5000")
    raw = {"pay_period": PERIOD, "employers": [batch()["employers"][0], batch()["employers"][2]]}
    out = run(PAYROLL_SPEC, raw, env)
    assert {k: c.state for k, c in children(out).items()} == {"a": "BLOCKED", "c": "SUCCEEDED"}
    assert any("not rate-locked" in r for r in children(out)["a"].proposal.reasons)
    assert env.money_ops(acct=env.accts["a"]) == []


def test_an_expired_lock_blocks_until_locked_again():
    env = make_env(a="20000")
    raw = one("a", de("anna", "100.00"))
    run(LOCK_SPEC, raw, env)
    env.fake.advance(3600)  # HR_1 quote has expired
    blocked = children(run(PAYROLL_SPEC, raw, env))["a"]  # judged in the child
    assert blocked.state == "BLOCKED"
    assert any("lock expires" in r for r in blocked.proposal.reasons)
    assert env.money_ops() == []
    out = pay(raw, env)
    assert children(out)["a"].state == "SUCCEEDED"


def test_a_lock_expiring_mid_run_sends_no_conversion():
    env = make_env(a="20000")
    raw = one("a", de("anna", "100.00"))
    run(LOCK_SPEC, raw, env)
    env.fake.advance(3400)  # proposal still has > 2 minutes of lock left
    env.fake.after_write["beneficiaries.create"] = lambda f, op: f.advance(600)
    out = run(PAYROLL_SPEC, raw, env)
    a = children(out)["a"]
    assert a.state == "FAILED"
    assert "rate lock expired" in a.detail
    assert env.money_ops() == []


def test_a_market_move_after_approval_never_makes_the_batch_stale():
    env = make_env(a="20000")
    raw = one("a", de("anna", "1000.00"))
    run(LOCK_SPEC, raw, env)
    eur = env.fake.price("EUR")
    out = run(PAYROLL_SPEC, raw, env, on_decide=lambda p: env.fake.set_price("EUR", eur * 2))
    assert out.state == "SUCCEEDED", out.detail
    lock = out.proposal.computed["children"][0]["fx_lock"]["EUR"]
    assert Money.of(env.fake.objects("conversions")[0]["sell_amount"], "USD") == lock["sell"]


def test_short_employer_escalates_while_others_proceed():
    env = make_env(a="20000", b="10", c="5000")
    out = pay(batch(), env)
    assert [children(out)[k].state for k in "abc"] == ["SUCCEEDED", "BLOCKED", "SUCCEEDED"]
    assert env.fake.balance("USD", env.accts["b"]) == Money.of("10", "USD")


def test_an_unknown_employer_is_refused_by_the_resolver_with_nothing_sent():
    raw = batch()
    raw["employers"].append(employer("zed", us("zoe", "10.00")))
    env = make_env(a="20000", b="20000", c="20000")
    out = run(PAYROLL_SPEC, raw, env)
    assert out.state == "FAILED"
    assert "unknown tenant" in out.detail
    assert env.money_ops() == []


def test_transfers_go_sent_then_paid():
    env = make_env(c="5000")
    out = pay(one("c", us("carl", "800.00")), env)
    assert children(out)["c"].state == "SUCCEEDED"
    sims = [op.body["next_status"] for op in env.fake.sent_ops if "transition" in op.name]
    assert sims == ["SENT", "PAID"]


def test_swift_cost_comes_from_the_transfer_read_back(monkeypatch):
    monkeypatch.setitem(fake_module.SWIFT_FEE, "EUR", Decimal("5.00"))
    env = make_env(a="20000")
    out = pay(one("a", de("anna", "100.00")), env)
    assert children(out)["a"].state == "SUCCEEDED", children(out)["a"].postcondition
    assert env.fake.balance("EUR", env.accts["a"]) == Money.of(
        "7.85", "EUR"
    )  # 12.85 sized, 5.00 charged


def test_a_deposit_during_the_opening_balance_read_is_not_counted_twice():
    env = make_env(c="5000")
    seen = {"txn": False, "done": False}

    def arm(f, req):
        seen["txn"] = True

    def dep(f, req):
        if seen["txn"] and not seen["done"] and req.on_behalf_of == env.accts["c"]:
            seen["done"] = True
            f.deposit(env.accts["c"], "250.00")

    env.fake.before_read["/api/v1/financial_transactions"] = arm
    env.fake.before_read["/api/v1/balances/current"] = dep
    out = pay(one("c", us("carl", "800.00")), env)
    assert seen["done"]
    assert children(out)["c"].state == "SUCCEEDED", children(out)["c"].postcondition


def test_timeout_after_submit_reconciles_and_never_pays_twice():
    env = make_env(a="20000")
    for name in ("transfers.create", "fx.conversions.create", "charges.create"):
        env.fake.inject(name, Fault.TIMEOUT_AFTER_SUBMIT)
    out = pay(batch_one("a"), env)
    assert children(out)["a"].state == "SUCCEEDED", children(out)["a"].detail
    assert len(env.fake.objects("transfers")) == 2
    assert len(env.fake.objects("conversions")) == 1
    assert len(env.fake.objects("charges")) == 1


def test_duplicate_delivery_creates_one_of_everything():
    env = make_env(a="20000")
    for name in ("transfers.create", "fx.conversions.create", "charges.create"):
        env.fake.inject(name, Fault.DUPLICATE_DELIVERY)
    out = pay(batch_one("a"), env)
    assert children(out)["a"].state == "SUCCEEDED", children(out)["a"].detail
    assert len(env.fake.objects("transfers")) == 2
    assert len(env.fake.objects("conversions")) == 1


def test_a_500_that_still_applies_lands_once():
    env = make_env(c="5000")
    env.fake.inject("transfers.create", Fault.SERVER_ERROR, applied=True)
    out = pay(one("c", us("carl", "800.00")), env)
    assert len(env.fake.objects("transfers")) == 1
    if children(out)["c"].state != "SUCCEEDED":  # in flight now; the next run finishes it
        out = run(PAYROLL_SPEC, one("c", us("carl", "800.00")), env)
    assert children(out)["c"].state == "SUCCEEDED", children(out)["c"].postcondition
    assert len(env.fake.objects("transfers")) == 1


def test_list_lag_after_a_create_does_not_pay_twice():
    env = make_env(c="5000")
    env.fake.inject("transfers.create", Fault.LIST_LAG, visible_after_s=20)
    out = pay(one("c", us("carl", "800.00")), env)
    if children(out)["c"].state != "SUCCEEDED":
        env.fake.advance(60)
        out = run(PAYROLL_SPEC, one("c", us("carl", "800.00")), env)
    assert children(out)["c"].state == "SUCCEEDED", children(out)["c"].postcondition
    assert len(env.fake.objects("transfers")) == 1


def test_a_429_then_a_crash_resumes_without_paying_twice():
    env = make_env(c="5000")
    env.fake.inject("transfers.create", Fault.RATE_LIMIT)
    env.fake.crash_after.add("transfers.create")
    out = pay(one("c", us("carl", "800.00")), env)
    if children(out)["c"].state != "SUCCEEDED":
        out = run(PAYROLL_SPEC, one("c", us("carl", "800.00")), env)
    assert children(out)["c"].state == "SUCCEEDED", children(out)["c"].postcondition
    assert len(env.fake.objects("transfers")) == 1


def test_rerun_of_the_same_pay_period_creates_no_second_payout():
    env = make_env(a="20000")
    assert children(pay(batch_one("a"), env))["a"].state == "SUCCEEDED"
    sent = len(env.money_ops())
    again = run(PAYROLL_SPEC, batch_one("a"), env)
    assert children(again)["a"].state == "SUCCEEDED", children(again)["a"].postcondition
    assert len(env.money_ops()) == sent


def test_a_builder_that_drops_the_header_is_refused_before_sending():
    from dataclasses import replace

    steps = tuple(
        replace(s, build=lambda c, b=s.build: [replace(o, on_behalf_of=None) for o in b(c)])
        if s.name == "fx_conversions"
        else s
        for s in EMPLOYER_SPEC.steps
    )
    spec = replace(PAYROLL_SPEC, child=replace(EMPLOYER_SPEC, steps=steps))
    env = make_env(a="20000")
    run(LOCK_SPEC, batch_one("a"), env)
    out = run(spec, batch_one("a"), env)
    assert children(out)["a"].state == "FAILED"
    assert "TenantScopeViolation" in children(out)["a"].detail
    assert env.money_ops("fx.conversions.create") == []


def test_fx_landing_in_the_platform_wallet_is_caught_by_the_postcondition():
    """The guide's trap: FX without the header converts the platform wallet."""
    env = make_env(a="20000")
    env.fake = env.fake  # same fake; platform funded so the misrouted conversion succeeds
    env.fake.credit("USD", "50000")  # the platform account
    run(LOCK_SPEC, batch_one("a"), env)
    env.fake.drop_obo.add("fx.conversions.create")
    out = run(PAYROLL_SPEC, batch_one("a"), env)
    a = children(out)["a"]
    assert a.state != "SUCCEEDED"
    assert a.postcondition is not None
    assert not a.postcondition.ok


def test_a_cancelled_payout_withholds_the_fee_and_reports_the_failure_code():
    env = make_env(a="20000")
    env.fake.fail_paid.add("anna")
    out = pay(batch_one("a"), env)
    a = children(out)["a"]
    assert a.state == "FAILED"
    assert "anna payout is CANCELLED (failure.code" in a.postcondition.detail
    assert env.fake.objects("charges") == []


def test_local_only_and_gbp_corridors_pay_end_to_end():
    env = make_env(b="20000", c="5000")
    raw = {
        "pay_period": PERIOD,
        "employers": [employer("b", gb("bea", "700.00")), employer("c", us("carl", "800.00"))],
    }
    out = pay(raw, env)
    assert {k: c.state for k, c in children(out).items()} == dict.fromkeys("bc", "SUCCEEDED")
    assert env.fake.balance("GBP", env.accts["b"]).is_zero()
    assert env.fake.balance("USD", env.accts["c"]) == Money.of("4188.00", "USD")


def test_a_large_payroll_needs_two_approvers():
    env = make_env(a="1000000")
    raw = one("a", us("alice", "9000.00"), de("anna", "9000.00"))
    run(LOCK_SPEC, raw, env)
    out = run(PAYROLL_SPEC, raw, env, approvers=("approver-1",))
    assert out.proposal.risk_tier is RiskTier.DUAL
    assert out.state == ApprovalState.EXPIRED.value
    assert env.money_ops() == []


def batch_one(code: str):
    return {
        "pay_period": PERIOD,
        "employers": [e for e in batch()["employers"] if e["employer"] == code],
    }


@pytest.mark.parametrize("code", ["a"])
def test_child_validate_is_idempotent_on_the_approved_child(code):
    env = make_env(a="20000")
    run(LOCK_SPEC, batch_one(code), env)
    p = run(PAYROLL_SPEC, batch_one(code), env, approvers=()).proposal
    child = p.computed["children"][0]
    assert EMPLOYER_SPEC.validate(child) == child
    assert EMPLOYER_SPEC.validate(child_raw(employer("a", us("x", "1.00"))))["pay_period"] == PERIOD
