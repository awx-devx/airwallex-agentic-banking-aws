"""SWIFT fee sizing for a large EUR payout, and the part-paid employer, on PayrollFake.

1. A 2,000 EUR SWIFT payout costs more than the EUR 6.21 seen on small payouts, so kit 6 sizes
   each EUR SWIFT payout at EUR 12.85 (pricing.py) and the conversion covers it.
2. When a payout is refused after an earlier payout of the same employer has left, the sends
   that left run finality (and the sandbox advance), the refused payout is not retried in that
   run, and the postcondition flags the employer PART-PAID for a person.
"""

from __future__ import annotations

from awx_common import Money
from awx_k6_payroll import PAYROLL_SPEC
from awx_k6_payroll.workflow import LOCK_SPEC
from awx_spine.fake import Fault
from k6_data import PERIOD, de, employer, us
from k6_fake import children, make_env, run


def _pay(raw, env):
    run(LOCK_SPEC, raw, env)
    return run(PAYROLL_SPEC, raw, env)


def test_a_2000_eur_swift_payout_is_converted_with_its_fee_and_paid():
    env = make_env(a="20000")  # the fake charges EUR 12.85 per SWIFT payout (conftest)
    raw = {"pay_period": PERIOD, "employers": [employer("a", de("anna", "2000.00"))]}
    out = _pay(raw, env)
    child = children(out)["a"]
    assert child.state == "SUCCEEDED", (child.detail, child.postcondition)
    [conv] = env.fake.objects("conversions")
    assert Money.of(conv["buy_amount"], "EUR") == Money.of("2012.85", "EUR")
    assert {t["status"] for t in env.fake.objects("transfers")} == {"PAID"}
    assert env.fake.balance("EUR", env.accts["a"]).is_zero()


def test_a_second_payout_refused_leaves_the_first_paid_and_flags_part_paid():
    env = make_env(a="20000")
    raw = {
        "pay_period": PERIOD,
        "employers": [employer("a", us("alice", "1200.00"), de("anna", "2000.00"))],
    }

    def refuse_next(fake, op):
        if not fake.objects("transfers")[1:]:  # after the first payout lands
            fake.inject("transfers.create", Fault.INSUFFICIENT_FUNDS)

    env.fake.after_write["transfers.create"] = refuse_next
    out = _pay(raw, env)
    child = children(out)["a"]
    assert child.state == "FAILED"
    assert "transfers.create FAILED" in (child.detail or "")
    [alice] = env.fake.objects("transfers")
    assert alice["reference"].endswith("-alice")
    assert alice["status"] == "PAID"  # finality and the sandbox advance ran for it
    sims = [o.body["next_status"] for o in env.fake.sent_ops if "transition" in o.name]
    assert sims == ["SENT", "PAID"]
    creates = [o for o in env.fake.sent_ops if o.name == "transfers.create"]
    assert len(creates) == 2  # anna once: never retried in this run
    assert not any(o.name == "charges.create" for o in env.fake.sent_ops)  # no fee withheld
    detail = child.postcondition.detail
    assert "PART-PAID, a person decides: paid ['alice'], unpaid ['anna']" in detail
