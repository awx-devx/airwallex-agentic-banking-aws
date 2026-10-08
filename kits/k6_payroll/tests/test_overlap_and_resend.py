"""Overlapping runs of one employer and pay period, re-emitted conversions, refused resends
and rate-lock edge cases, all through run_kit_workflow on FakeAirwallex."""

from datetime import UTC, datetime

from awx_common import Money, OperationStatus
from awx_k6_payroll import PAYROLL_SPEC
from awx_k6_payroll import workflow as wf
from awx_k6_payroll.books import attempt, read_lock, sent
from awx_k6_payroll.workflow import LOCK_SPEC, keys_for, validate_batch
from awx_spine.fake import Fault
from awx_spine.store import InMemoryOperationStore
from k6_data import PERIOD, de, employer, gb, us
from k6_fake import children, make_env, run

DAY = 86400


def one(code, *lines):
    return {"pay_period": PERIOD, "employers": [employer(code, *lines)]}


def carl():
    return one("c", us("carl", "800.00"))


def anna():
    return one("a", de("anna", "100.00"))


def transfer_rids(env, acct=None):
    return [op.request_id for op in env.money_ops("transfers.create", acct)]


def paid_for(env, contractor):
    return [
        t
        for t in env.fake.objects("transfers")
        if t["status"] == "PAID" and str(t["reference"]).endswith(f"-{contractor}")
    ]


# ---------------------------------------------------------------- overlapping runs


def test_an_overlapping_proposal_for_the_same_employer_and_period_is_refused():
    """P1's first send of carl's payout is refused; before P1's postcondition runs, a separately
    approved P2 (a batch that also lists c) runs. Without a claim P2 lands the same request_id,
    P1 then records it as a definitive failure, and P3 pays carl again under attempt 2. With the
    per-employer, per-period claim P2's child for c is BLOCKED and carl is paid once."""
    env = make_env(b="20000", c="5000")
    env.fake.inject("transfers.create", Fault.INSUFFICIENT_FUNDS)
    nested = {}

    def overlap(fake, op):
        if "p2" not in nested and op.on_behalf_of == env.accts["c"]:
            both = {
                "pay_period": PERIOD,
                "employers": [
                    employer("b", us("bob", "900.00")),
                    employer("c", us("carl", "800.00")),
                ],
            }
            nested["p2"] = run(PAYROLL_SPEC, both, env, proposed_by="scheduler-2")

    env.fake.after_write["transfers.create"] = overlap
    p1 = children(run(PAYROLL_SPEC, carl(), env))["c"]
    assert p1.state == "FAILED"
    p2 = children(nested["p2"])
    assert p2["b"].state == "SUCCEEDED", p2["b"].postcondition
    assert p2["c"].state == "BLOCKED"
    assert any("another run of this period is in flight" in r for r in p2["c"].proposal.reasons)
    env.fake.after_write.clear()
    p3 = children(run(PAYROLL_SPEC, carl(), env))["c"]
    assert p3.state == "SUCCEEDED", p3.postcondition
    assert len(paid_for(env, "carl")) == 1


def test_two_runs_that_both_pass_policy_cannot_both_send(monkeypatch):
    """Backstop: the claim is a conditional create in the snapshot, so the second run fails
    closed before sending even if its policy did not see the first run's claim."""
    env = make_env(c="5000")
    nested = {}

    def overlap(fake, op):
        if "p2" not in nested:
            monkeypatch.setattr(wf, "read_claim", lambda store, k: None)
            nested["p2"] = run(PAYROLL_SPEC, carl(), env, proposed_by="scheduler-2")
            monkeypatch.undo()

    env.fake.after_write["beneficiaries.create"] = overlap
    p1 = children(run(PAYROLL_SPEC, carl(), env))["c"]
    assert p1.state == "SUCCEEDED", p1.postcondition
    p2 = children(nested["p2"])["c"]
    assert p2.state == "FAILED"
    assert "RunClaimed" in p2.detail
    assert len(transfer_rids(env)) == 1
    assert len(paid_for(env, "carl")) == 1


def test_the_owner_releases_its_claim_so_the_next_period_run_is_not_blocked():
    env = make_env(c="5000")
    assert children(run(PAYROLL_SPEC, carl(), env))["c"].state == "SUCCEEDED"
    k = keys_for(env.platform_store, children(run(PAYROLL_SPEC, carl(), env))["c"].proposal.intent)
    assert wf.read_claim(env.platform_store, k) is None


# ---------------------------------------------------------------- a re-emission equals its approval


def test_a_re_emitted_conversion_binds_exactly_what_it_was_sent_with():
    """The AUTO lock re-quotes between P1's recheck and its conversion build; P1 sends the old quote
    and the outcome is unknown. The next approval must bind the in-flight conversion's quote and
    sell, never the newer lock."""
    env = make_env(a="20000")
    run(LOCK_SPEC, anna(), env, approvers=())
    env.fake.inject("fx.conversions.create", Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=30 * DAY)
    first = children(run(PAYROLL_SPEC, anna(), env))["a"]
    assert first.state != "SUCCEEDED"
    sent_op = env.money_ops("fx.conversions.create")[0]
    k = keys_for(env.platform_store, first.proposal.intent)
    # A newer lock for the same attempt (with keep-until-expired the AUTO action cannot write
    # one while the old is valid, so plant it directly).
    key = f"lock/{k.book}:{k.employer}:{k.period}/EUR"
    item = env.platform_store.get(key)
    env.platform_store.put(
        key,
        {**item.value, "quote_id": "newer-quote", "sell": item.value["sell"] + item.value["sell"]},
        expected_version=item.version,
    )
    newer = read_lock(env.platform_store, k, "EUR")
    assert newer["quote_id"] != sent_op.body["quote_id"]  # the lock did move on

    second = run(PAYROLL_SPEC, anna(), env)
    bound = second.proposal.computed["children"][0]["fx_lock"]["EUR"]
    assert bound["quote_id"] == sent_op.body["quote_id"]
    assert bound["sell"] == first.proposal.intent["fx_lock"]["EUR"]["sell"]
    re_emits = env.money_ops("fx.conversions.create")
    assert {op.body["quote_id"] for op in re_emits} == {sent_op.body["quote_id"]}
    assert {op.request_id for op in re_emits} == {sent_op.request_id}


def test_a_re_emission_that_differs_from_the_approval_is_refused_with_nothing_sent():
    env = make_env(a="20000")
    run(LOCK_SPEC, anna(), env, approvers=())
    env.fake.inject("fx.conversions.create", Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=30 * DAY)
    first = children(run(PAYROLL_SPEC, anna(), env))["a"]
    sent_before = len(env.money_ops())
    child = dict(first.proposal.intent)
    lock = dict(child["fx_lock"]["EUR"])
    lock["sell"] = lock["sell"] + Money.of("1.00", "USD")  # an approval that disagrees
    child["fx_lock"] = {"EUR": lock}
    from types import SimpleNamespace

    sctx = SimpleNamespace(
        intent=child,
        policy=SimpleNamespace(
            platform_store=env.platform_store,
            store=env.kit_store,
            now=env.fake.now(),
            read=lambda req: env.fake.read(req.__class__(req.path, req.query, env.accts["a"])),
            tenant=first.proposal and env.resolver.resolve("k6:employer:a", kit="k6"),
        ),
        proposal_id=first.proposal.proposal_id,
        intent_hash="0" * 64,
    )
    try:
        wf.build_conversions(sctx)
        raised = None
    except wf.QuoteAboveApprovedCap as exc:
        raised = str(exc)
    assert raised is not None
    assert "approval binds" in raised
    assert len(env.money_ops()) == sent_before


# ---------------------------------------------------------------- refused resends


def lose_first_send(env, raw, op_name):
    env.fake.inject(op_name, Fault.SERVER_ERROR, times=50)  # 500, not applied: unknown
    out = run(PAYROLL_SPEC, raw, env)
    env.fake.clear_faults(op_name)
    return out


def test_a_refused_resend_of_an_unknown_payout_stays_in_flight():
    """Spine path: run 1's payout ends unknown with an empty read-back; run 2 re-emits it and
    is refused. The executor keeps it OUTCOME_UNKNOWN with
    body["last_error"], the kit records no failure, and no second request_id is ever sent."""
    env = make_env(c="5000")
    first = children(lose_first_send(env, carl(), "transfers.create"))["c"]
    assert first.state == "FAILED"
    env.fake.inject("transfers.create", Fault.INSUFFICIENT_FUNDS, times=50)
    second = children(run(PAYROLL_SPEC, carl(), env))["c"]
    env.fake.clear_faults("transfers.create")
    refused = [r for r in second.results.get("payouts", ()) if r.request_id in transfer_rids(env)]
    assert refused[-1].status is OperationStatus.OUTCOME_UNKNOWN
    assert refused[-1].body["last_error"]["error_type"] == "InsufficientFunds"
    k = keys_for(env.platform_store, second.proposal.intent)
    assert attempt(env.platform_store, k.payout_key("carl")) == 1
    assert "in flight" in second.postcondition.detail
    third = children(run(PAYROLL_SPEC, carl(), env))["c"]
    assert third.state == "SUCCEEDED", third.postcondition
    assert len(set(transfer_rids(env))) == 1


def test_a_refused_resend_stays_in_flight_when_the_operation_store_does_not_know():
    """The same case with no operation record (a fresh store): the executor reports the
    refusal FAILED, so only the kit's first_send and per-attempt run_token keep the money on
    its request_id. Ignoring first_send, or reusing one token across attempts, pays carl
    under attempt 2 here."""
    env = make_env(c="5000")
    lose_first_send(env, carl(), "transfers.create")
    env.ops = InMemoryOperationStore()
    env.fake.inject("transfers.create", Fault.INSUFFICIENT_FUNDS)
    second = children(run(PAYROLL_SPEC, carl(), env))["c"]
    payouts = second.results.get("payouts", ())
    assert payouts[-1].status is OperationStatus.FAILED  # the spine cannot know
    k = keys_for(env.platform_store, second.proposal.intent)
    assert attempt(env.platform_store, k.payout_key("carl")) == 1
    # The fresh store now holds a certain FAILED for that request_id, so the money stays on
    # it: later runs re-emit nothing new, and past the margin a person decides (rule M1).
    third = children(run(PAYROLL_SPEC, carl(), env))["c"]
    assert third.state == "FAILED"
    assert attempt(env.platform_store, k.payout_key("carl")) == 1
    env.fake.advance(7 * DAY)
    fourth = children(run(PAYROLL_SPEC, carl(), env))["c"]
    assert fourth.state == "BLOCKED"
    assert any("NEEDS_PERSON" in r for r in fourth.proposal.reasons)
    assert len(set(transfer_rids(env))) == 1
    assert paid_for(env, "carl") == []


def test_a_refused_resend_of_an_unknown_conversion_stays_on_attempt_1():
    env = make_env(a="20000")
    run(LOCK_SPEC, anna(), env, approvers=())
    lose_first_send(env, anna(), "fx.conversions.create")
    env.ops = InMemoryOperationStore()
    env.fake.inject("fx.conversions.create", Fault.INSUFFICIENT_FUNDS)
    second = children(run(PAYROLL_SPEC, anna(), env))["a"]
    assert second.results["fx_conversions"][-1].status is OperationStatus.FAILED
    k = keys_for(env.platform_store, second.proposal.intent)
    assert attempt(env.platform_store, k.conversion_key("EUR", "USD")) == 1
    assert len({op.request_id for op in env.money_ops("fx.conversions.create")}) == 1


def test_a_first_send_refused_at_create_is_definitive_and_moves_to_attempt_2():
    env = make_env(c="5000")
    env.fake.inject("transfers.create", Fault.INSUFFICIENT_FUNDS)
    first = children(run(PAYROLL_SPEC, carl(), env))["c"]
    assert first.state == "FAILED"
    k = keys_for(env.platform_store, first.proposal.intent)
    assert attempt(env.platform_store, k.payout_key("carl")) == 2
    second = children(run(PAYROLL_SPEC, carl(), env))["c"]
    assert second.state == "SUCCEEDED", second.postcondition
    assert len(set(transfer_rids(env))) == 2
    assert len(paid_for(env, "carl")) == 1


def test_two_objects_under_one_request_id_block_without_a_new_attempt():
    env = make_env(c="5000")
    env.fake.inject("transfers.create", Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=DAY)
    run(PAYROLL_SPEC, carl(), env)
    op = env.money_ops("transfers.create")[0]
    env.fake._state.request_ids.clear()  # a provider-side dedupe miss
    super(type(env.fake), env.fake).execute(op)
    env.fake.advance(DAY + 1)
    second = children(run(PAYROLL_SPEC, carl(), env))["c"]
    assert second.state == "BLOCKED"
    assert any("twice" in r for r in second.proposal.reasons)
    k = keys_for(env.platform_store, second.proposal.intent)
    assert attempt(env.platform_store, k.payout_key("carl")) == 1
    assert len(set(transfer_rids(env))) == 1


# ---------------------------------------------------------------- rate-lock edge cases


def test_a_re_lock_during_approval_keeps_the_lock_and_never_voids_it():
    env = make_env(a="20000")
    run(LOCK_SPEC, anna(), env, approvers=())
    out = run(
        PAYROLL_SPEC,
        anna(),
        env,
        on_decide=lambda p: run(LOCK_SPEC, anna(), env, approvers=()),
    )
    assert out.state == "SUCCEEDED", out.detail
    assert children(out)["a"].state == "SUCCEEDED"


def test_a_supplied_fx_lock_never_reaches_the_approval():
    raw = anna()
    raw["employers"][0]["fx_lock"] = {
        "EUR": {
            "quote_id": "forged",
            "sell": Money.of("0.01", "USD"),
            "valid_to_at": datetime(2030, 1, 1, tzinfo=UTC),
        }
    }
    import pytest
    from awx_k6_payroll.model import PayrollInputError

    with pytest.raises(PayrollInputError, match="unknown fields"):
        validate_batch(raw)  # parse_batch refuses it before the strip, which is the backstop


def test_each_sent_conversion_records_its_own_currencys_lock_expiry():
    env = make_env(b="20000")
    raw = {
        "pay_period": PERIOD,
        "employers": [employer("b", de("bernd", "100.00"), gb("bea", "100.00"))],
    }
    run(LOCK_SPEC, raw, env, approvers=())
    k = keys_for(env.platform_store, {"employer": "b", "pay_period": PERIOD})
    eur_lock = read_lock(env.platform_store, k, "EUR")
    # Re-lock GBP 20 minutes later (EUR kept), so the two locks expire at different times.
    env.fake.advance(1200)
    gbp_key = f"lock/{k.book}:{k.employer}:{k.period}/GBP"
    item = env.platform_store.get(gbp_key)
    env.platform_store.put(
        gbp_key,
        {**item.value, "valid_to_at": eur_lock["valid_to_at"].replace(minute=20)},
        expected_version=item.version,
    )
    gbp_lock = read_lock(env.platform_store, k, "GBP")
    assert gbp_lock["valid_to_at"] != eur_lock["valid_to_at"]
    env.fake.inject(
        "fx.conversions.create", Fault.TIMEOUT_AFTER_SUBMIT, times=2, visible_after_s=30 * DAY
    )
    run(PAYROLL_SPEC, raw, env)
    for ccy, lock in (("EUR", eur_lock), ("GBP", gbp_lock)):
        rec = sent(env.platform_store, k.conversion_rid(ccy, "USD"))
        assert rec is not None
        assert rec["valid_to_at"] == lock["valid_to_at"], ccy
