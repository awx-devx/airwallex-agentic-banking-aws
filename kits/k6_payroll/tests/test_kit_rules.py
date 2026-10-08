"""Kit rules M1 to M4, attempts after a definitive failure, retryable refusals and uncertain
sends, through the real runner."""

from datetime import timedelta

import pytest
from awx_common import OperationResult, OperationStatus
from awx_k6_payroll import PAYROLL_SPEC, ids
from awx_k6_payroll import workflow as wf
from awx_k6_payroll.books import BOOK_KEY, read_book
from awx_k6_payroll.workflow import LOCK_SPEC, definitive_create_failure
from awx_spine.fake import Fault
from k6_data import PERIOD, batch, de, employer, us
from k6_fake import children, make_env, run

DAY = 86400


def one(code, *lines):
    return {"pay_period": PERIOD, "employers": [employer(code, *lines)]}


def carl():
    return one("c", us("carl", "800.00"))


def anna():
    return one("a", de("anna", "100.00"))


def pay(raw, env, **kw):
    run(LOCK_SPEC, raw, env)
    return children(run(PAYROLL_SPEC, raw, env, **kw))


def rids(env, name):
    return {op.request_id for op in env.money_ops(name)}


# ---------------------------------------------------------------- M2


def test_two_books_seeded_the_same_way_produce_disjoint_request_ids():
    envs = [make_env(a="20000", b="20000", c="20000") for _ in range(2)]
    seen = []
    for env in envs:
        assert set(pay(batch(), env)) == {"a", "b", "c"}
        # Kit-built ids only: awx_spine's sandbox transitions key on the provider's transfer id,
        # which this deterministic fake repeats across instances (a live sandbox does not).
        seen.append(
            {
                op.request_id
                for op in env.fake.sent_ops
                if op.name != "transfers.simulate_transition"
            }
        )
        assert ids.BOOK.match(read_book(env.platform_store))
    assert read_book(envs[0].platform_store) != read_book(envs[1].platform_store)
    assert seen[0].isdisjoint(seen[1])
    nicks = [{b["nickname"] for b in e.fake.objects("beneficiaries")} for e in envs]
    assert nicks[0].isdisjoint(nicks[1])


def test_the_book_is_minted_once_and_never_a_constant():
    env = make_env(c="5000")
    assert read_book(env.platform_store) is None
    pay(carl(), env)
    first = env.platform_store.get(BOOK_KEY)
    pay(carl(), env)
    assert env.platform_store.get(BOOK_KEY) == first
    for bad in ("demo", "", "0" * 31):
        with pytest.raises(ValueError, match="minted"):
            ids.payout_request_id(bad, "a", "alice", PERIOD, 1)


# ---------------------------------------------------------------- M1 and attempts


def test_a_conversion_refused_at_create_moves_to_attempt_2_after_a_fresh_lock():
    env = make_env(a="20000")
    env.fake.inject("fx.conversions.create", Fault.INSUFFICIENT_FUNDS)
    first = pay(anna(), env)["a"]
    assert first.state == "FAILED"
    assert "definitive failures recorded" in first.postcondition.detail
    # The old lock was for attempt 1, so the payroll blocks until locked again.
    stale = run(PAYROLL_SPEC, anna(), env)
    assert stale.state == "BLOCKED"
    assert any("earlier attempt" in r for r in stale.proposal.reasons)
    second = pay(anna(), env)["a"]
    assert second.state == "SUCCEEDED", second.postcondition
    convs = env.money_ops("fx.conversions.create")
    assert len({op.request_id for op in convs}) == 2
    assert len({op.body["quote_id"] for op in convs}) == 2
    assert len(env.fake.objects("conversions")) == 1


def test_an_invisible_payout_stays_in_flight_and_lands_once():
    """M1: a write that landed at the timeout but is invisible to ?request_id= and GET."""
    env = make_env(c="5000")
    env.fake.inject("transfers.create", Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=3600)
    first = pay(carl(), env)["c"]
    assert first.state != "SUCCEEDED"
    assert "definitive" not in (first.postcondition.detail if first.postcondition else "")
    env.fake.advance(3700)
    second = children(run(PAYROLL_SPEC, carl(), env))["c"]
    assert second.state == "SUCCEEDED", second.postcondition
    assert len(rids(env, "transfers.create")) == 1
    assert len(env.fake.objects("transfers")) == 1


def test_resends_that_only_get_429_never_start_attempt_2():
    """The first send times out and lands invisibly, every resend gets 429, then
    the original becomes visible. A retryable FAILED is money in flight: one payment."""
    env = make_env(c="5000")
    env.fake.inject("transfers.create", Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=1800)
    env.fake.inject("transfers.create", Fault.RATE_LIMIT, times=50)
    first = pay(carl(), env)["c"]
    assert first.state != "SUCCEEDED"
    env.fake.advance(2000)
    env.fake.clear_faults("transfers.create")  # the 429s stop
    second = children(run(PAYROLL_SPEC, carl(), env))["c"]
    assert second.state == "SUCCEEDED", second.postcondition
    assert len(rids(env, "transfers.create")) == 1  # no attempt 2
    assert len(env.fake.objects("transfers")) == 1


@pytest.mark.parametrize(
    ("result", "first"),
    [
        (OperationResult("r", OperationStatus.FAILED, 429, error_type="RateLimited"), True),
        (
            OperationResult("r", OperationStatus.FAILED, 503, error_type="TransientServerError"),
            True,
        ),
        (OperationResult("r", OperationStatus.FAILED, 400, error_type="DuplicateRequest"), True),
        (OperationResult("r", OperationStatus.FAILED, 400, error_type="InsufficientFunds"), False),
        # A refused resend and two objects under one id both come back unknown.
        (
            OperationResult(
                "r",
                OperationStatus.OUTCOME_UNKNOWN,
                400,
                body={"refused": {"code": "duplicate_request_id"}},
                error_type="OutcomeUnknown",
            ),
            True,
        ),
        (
            OperationResult(
                "r",
                OperationStatus.OUTCOME_UNKNOWN,
                error_type="OutcomeUnknown",
                error_code="multiple_objects",
            ),
            True,
        ),
    ],
)
def test_only_a_first_sends_non_retryable_refusal_is_definitive(result, first):
    assert definitive_create_failure(result, first_send=first) is False
    ok = OperationResult("r", OperationStatus.FAILED, 400, error_type="InsufficientFunds")
    assert definitive_create_failure(ok, first_send=True) is True


def test_a_cancelled_payout_is_retried_under_attempt_2_on_the_next_approved_run():
    env = make_env(a="20000")
    env.fake.fail_paid.add("anna")
    assert pay(anna(), env)["a"].state == "FAILED"
    env.fake.fail_paid.clear()
    second = children(run(PAYROLL_SPEC, anna(), env))["a"]
    assert second.state == "SUCCEEDED", second.postcondition
    assert len(rids(env, "transfers.create")) == 2
    assert sorted(t["status"] for t in env.fake.objects("transfers")) == ["CANCELLED", "PAID"]


# ---------------------------------------------------------------- M1 window


def lose_payout(env, raw):
    env.fake.inject("transfers.create", Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=30 * DAY)
    out = pay(raw, env)
    assert all(c.state != "SUCCEEDED" for c in out.values())


def test_an_in_flight_payout_inside_the_margin_is_re_emitted_and_lands_once():
    env = make_env(c="5000")
    lose_payout(env, carl())
    env.fake.advance(5 * DAY)
    env.fake.clear_faults()
    out = children(run(PAYROLL_SPEC, carl(), env))["c"]
    assert len(rids(env, "transfers.create")) == 1
    assert len(env.fake.objects("transfers")) == 1
    assert out.state in ("SUCCEEDED", "FAILED")  # invisible original keeps it in flight


def test_past_the_margin_a_person_decides_and_the_rest_of_the_batch_runs():
    env = make_env(b="20000", c="5000")
    raw = {"pay_period": PERIOD, "employers": [employer("c", us("carl", "800.00"))]}
    lose_payout(env, raw)
    env.fake.advance(7 * DAY)
    sent = len(env.money_ops(acct=env.accts["c"]))
    both = {
        "pay_period": PERIOD,
        "employers": [employer("b", us("bob", "900.00")), employer("c", us("carl", "800.00"))],
    }
    out = children(run(PAYROLL_SPEC, both, env))
    assert out["b"].state == "SUCCEEDED", out["b"].postcondition
    assert out["c"].state == "BLOCKED"
    line = next(r for r in out["c"].proposal.reasons if r.startswith("NEEDS_PERSON"))
    assert "employer c" in line
    assert PERIOD in line
    assert "request_id" in line
    assert len(env.money_ops(acct=env.accts["c"])) == sent  # nothing sent for C


def test_money_routed_to_a_person_cannot_be_paid_another_way(monkeypatch):
    env = make_env(c="5000")
    lose_payout(env, carl())
    env.fake.advance(7 * DAY)
    monkeypatch.setattr(wf, "in_flight_needing_person", lambda intent, ctx: [])
    gated = children(run(PAYROLL_SPEC, carl(), env))["c"]
    assert gated.state == "FAILED"
    assert "NeedsPerson" in gated.detail
    monkeypatch.undo()
    for _ in range(2):
        again = children(run(PAYROLL_SPEC, carl(), env))["c"]
        assert again.state == "BLOCKED"
    assert len(rids(env, "transfers.create")) == 1


# ---------------------------------------------------------------- run_token per attempt


def test_each_attempt_of_one_proposal_id_gets_its_own_run_token():
    """Rerunning the same proposal (same window, same computed, same proposal_id) is a new
    attempt: the finished snapshot gets a new token, so the earlier attempt's sends count as
    re-emissions, never as this attempt's first sends."""
    env = make_env(c="5000")
    pay(carl(), env)
    (first,) = env.kit_store.query("snapshot/")
    assert first.value["finished"] is True
    run(PAYROLL_SPEC, carl(), env)
    (again,) = env.kit_store.query("snapshot/")
    assert again.key == first.key  # same proposal_id
    assert again.value["run_token"] != first.value["run_token"]
    assert again.value["finished"] is True


# ---------------------------------------------------------------- paging


def test_financial_transactions_are_read_page_by_page(monkeypatch):
    env = make_env(c="5000")
    monkeypatch.setattr(wf, "TXN_PAGE_SIZE", 10)
    for n in range(25):
        env.fake.deposit(env.accts["c"], f"{n + 1}.00")
    out = pay(carl(), env)["c"]
    assert out.state == "SUCCEEDED", out.postcondition
    reads = [r for r in env.fake.requests if r[1] == "/api/v1/financial_transactions"]
    assert len(reads) >= 3 * 3  # each listing walks 3 pages of 10 (snapshot x2, postcondition)


def test_more_pages_than_the_bound_fail_closed_before_any_money_moves(monkeypatch):
    env = make_env(c="5000")
    monkeypatch.setattr(wf, "TXN_PAGE_SIZE", 10)
    for _ in range(25):
        env.fake.deposit(env.accts["c"], "1.00")
    monkeypatch.setattr(wf, "MAX_TXN_PAGES", 2)
    out = pay(carl(), env)["c"]
    assert out.state == "FAILED"
    assert "more than 2 pages" in out.detail
    assert env.money_ops() == []


def test_m4_kit_6_takes_no_holds_and_has_no_on_abandon():
    assert PAYROLL_SPEC.on_abandon is None
    assert PAYROLL_SPEC.child.on_abandon is None
    assert LOCK_SPEC.on_abandon is None
    assert timedelta(days=6) == wf.DEDUP_SAFE_MARGIN
