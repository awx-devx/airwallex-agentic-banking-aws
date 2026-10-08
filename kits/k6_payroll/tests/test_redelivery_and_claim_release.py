"""Rule 0 (every build run twice), a mismatched object under a kit request_id as money in
flight, the operator's claim release, a tampered sent record, a resumed child and kit-bound
tenant rows. All through run_kit_workflow."""

from dataclasses import replace
from decimal import Decimal

import pytest
from awx_common import Money, Operation, OperationStatus
from awx_k6_payroll import PAYROLL_SPEC
from awx_k6_payroll import workflow as wf
from awx_k6_payroll.books import attempt, read_claim, sent
from awx_k6_payroll.release import RELEASE_SPEC
from awx_k6_payroll.workflow import EMPLOYER_LOCK_SPEC, EMPLOYER_SPEC, LOCK_SPEC, keys_for
from awx_spine.fake import Fault
from k6_data import PERIOD, batch, de, employer, us
from k6_fake import children, make_env, run

DAY = 86400


def anna():
    return {"pay_period": PERIOD, "employers": [employer("a", de("anna", "100.00"))]}


def carl():
    return {"pay_period": PERIOD, "employers": [employer("c", us("carl", "800.00"))]}


def twice(spec, step_name):
    """Rule 0: the step's build runs twice in one task and the first result is thrown away,
    as a redelivered build task does."""

    def build(ctx, orig):
        orig(ctx)
        return orig(ctx)

    steps = tuple(
        replace(s, build=lambda c, o=s.build: build(c, o)) if s.name == step_name else s
        for s in spec.steps
    )
    return replace(spec, steps=steps)


PAY_STEPS = [s.name for s in EMPLOYER_SPEC.steps]


@pytest.mark.parametrize("step", PAY_STEPS)
def test_rule_0_every_payroll_build_twice_sends_once_and_releases_its_claim(step):
    env = make_env(a="20000", b="20000", c="20000")
    run(LOCK_SPEC, batch(), env, approvers=())
    root = replace(PAYROLL_SPEC, child=twice(EMPLOYER_SPEC, step))
    out = run(root, batch(), env)
    assert out.state == "SUCCEEDED", out.detail
    for code, child in children(out).items():
        assert child.state == "SUCCEEDED", (code, child.postcondition)
        assert child.postcondition.ok
        k = keys_for(env.platform_store, child.proposal.intent)
        assert read_claim(env.platform_store, k) is None  # no stranded claim
    rids = [op.request_id for op in env.money_ops()]
    assert len(rids) == len(set(rids))  # every money op sent once
    assert len(env.fake.objects("transfers")) == 6
    assert len(env.fake.objects("charges")) == 3


def test_rule_0_the_lock_build_twice_stores_one_lock_per_currency():
    env = make_env(a="20000")
    root = replace(LOCK_SPEC, child=twice(EMPLOYER_LOCK_SPEC, "quotes"))
    out = run(root, anna(), env, approvers=())
    assert out.state == "SUCCEEDED", out.detail
    k = keys_for(env.platform_store, {"employer": "a", "pay_period": PERIOD})
    assert len(env.platform_store.query(f"lock/{k.book}")) == 1
    assert children(run(PAYROLL_SPEC, anna(), env))["a"].state == "SUCCEEDED"


def test_rule_0_the_release_build_twice_releases_once():
    env = make_env(c="5000")
    _hold_stale_claim(env)
    root = twice(RELEASE_SPEC, "release")
    out = run(root, REQ, env)
    assert out.state == "SUCCEEDED", out.detail


# ---------------------------------------------------------------- mismatched object


def _plant_foreign_payout(env, *, invisible_s: float | None):
    """Before carl's payout is sent, someone else's transfer lands under its request_id."""

    def plant(fake, op):
        if getattr(fake, "_planted", False):
            return
        fake._planted = True
        k = keys_for(env.platform_store, {"employer": "c", "pay_period": PERIOD})
        rid = k.payout_rid("carl")
        foreign = Operation(
            kit="k6",
            name="transfers.create",
            method="POST",
            path="/api/v1/transfers/create",
            body={
                "request_id": rid,
                "beneficiary_id": fake.objects("beneficiaries")[0]["id"],
                "transfer_currency": "USD",
                "transfer_amount": Decimal("1.00"),
                "source_currency": "USD",
                "transfer_method": "LOCAL",
                "reason": "other",
                "reference": "someone-else",
            },
            request_id=rid,
            business_key="foreign",
            intent_hash="0" * 64,
            on_behalf_of=env.accts["c"],
        )
        if invisible_s is not None:
            fake.inject("transfers.create", Fault.LIST_LAG, visible_after_s=invisible_s)
        type(fake).__mro__[1].execute(fake, foreign)  # FakeAirwallex.execute, not the hook

    env.fake.after_write["beneficiaries.create"] = plant


def test_a_mismatched_object_the_kit_can_see_fails_closed_before_any_send():
    env = make_env(c="5000")
    _plant_foreign_payout(env, invisible_s=None)
    first = children(run(PAYROLL_SPEC, carl(), env))["c"]
    assert first.state == "FAILED"
    assert "RequestIdBoundElsewhere" in first.detail
    assert env.money_ops("transfers.create") == []
    second = children(run(PAYROLL_SPEC, carl(), env))["c"]
    assert second.state == "BLOCKED"
    assert any("does not match" in r for r in second.proposal.reasons)


def test_a_mismatched_object_found_by_the_spine_is_money_in_flight():
    """The object is invisible to the kit's read, so carl's payout is sent;
    the spine finds the foreign object under the request_id and returns OUTCOME_UNKNOWN
    request_id_reused with no top-level provider_id. Kit 6 records no failure: no attempt 2,
    one request_id, and the next run is BLOCKED for a person."""
    env = make_env(c="5000")
    _plant_foreign_payout(env, invisible_s=2)
    first = children(run(PAYROLL_SPEC, carl(), env))["c"]
    assert first.state != "SUCCEEDED"
    last = first.results["payouts"][-1]
    assert last.status is OperationStatus.OUTCOME_UNKNOWN
    assert last.error_code == "request_id_reused"
    assert last.provider_id is None
    k = keys_for(env.platform_store, first.proposal.intent)
    assert attempt(env.platform_store, k.payout_key("carl")) == 1
    second = children(run(PAYROLL_SPEC, carl(), env))["c"]
    assert second.state == "BLOCKED"
    assert len({op.request_id for op in env.money_ops("transfers.create")}) == 1
    assert [t["reference"] for t in env.fake.objects("transfers")] == ["someone-else"]


# ---------------------------------------------------------------- operator claim release

ROOT = "dead-root-1"
REQ = {"employer": "c", "pay_period": PERIOD, "reason": "the payroll run died"}


def _hold_stale_claim(env, status="SUCCEEDED", root=ROOT, root_tenant="k6:operator"):
    """A claim left by a payroll whose root run is `root`, which run_status reports `status`."""
    from awx_k6_payroll.books import ensure_book, take_claim

    ensure_book(env.platform_store)
    k = keys_for(env.platform_store, {"employer": "c", "pay_period": PERIOD})
    take_claim(
        env.platform_store,
        k,
        "dead-child",
        env.fake.now(),
        root_proposal_id=root,
        root_action="run_payroll",
    )
    if status is not None:
        env.runs.set("k6", "run_payroll", root, status, tenant_id=root_tenant)
    return k


def test_a_release_blocks_while_the_root_run_reads_running_and_writes_nothing():
    env = make_env(c="5000")
    k = _hold_stale_claim(env, "RUNNING")
    before = {i.key: i.version for i in env.platform_store.query("")}
    out = run(RELEASE_SPEC, REQ, env)
    assert out.state == "BLOCKED"
    assert any("still RUNNING" in r for r in out.proposal.reasons)
    assert {i.key: i.version for i in env.platform_store.query("")} == before
    assert read_claim(env.platform_store, k)["owner"] == "dead-child"


def test_a_release_blocks_when_the_root_run_cannot_be_found():
    env = make_env(c="5000")
    k = _hold_stale_claim(env, None)  # purged, or never deployed
    out = run(RELEASE_SPEC, REQ, env)
    assert out.state == "BLOCKED"
    assert any("no root run" in r for r in out.proposal.reasons)
    assert read_claim(env.platform_store, k) is not None


def test_a_claim_whose_root_is_terminal_is_released_and_the_payroll_then_pays():
    env = make_env(c="5000")
    k = _hold_stale_claim(env, "FAILED")
    assert children(run(PAYROLL_SPEC, carl(), env))["c"].state == "BLOCKED"
    assert run(RELEASE_SPEC, REQ, env, approvers=()).state == "EXPIRED"  # SINGLE, never AUTO
    released = run(RELEASE_SPEC, REQ, env, origin=wf_origin_agent(), proposed_by="model")
    assert released.state == "SUCCEEDED", released.detail
    assert read_claim(env.platform_store, k) is None
    assert env.money_ops() == []
    paid = children(run(PAYROLL_SPEC, carl(), env))["c"]
    assert paid.state == "SUCCEEDED", paid.postcondition


def test_a_real_payroll_records_its_root_in_the_claim(monkeypatch):
    """The claim names the payroll's root run (root_proposal_id), so a release asks the
    right run; the local root reads SUCCEEDED once its audit is written."""
    env = make_env(c="5000")
    monkeypatch.setattr(wf, "release_claim", lambda *a, **k: None)  # the owner dies
    first = run(PAYROLL_SPEC, carl(), env)
    monkeypatch.undo()
    k = keys_for(env.platform_store, children(first)["c"].proposal.intent)
    claim = read_claim(env.platform_store, k)
    assert claim["root_proposal_id"] == first.proposal.proposal_id
    assert claim["root_action"] == "run_payroll"
    assert run(RELEASE_SPEC, REQ, env).state == "SUCCEEDED"
    assert read_claim(env.platform_store, k) is None


def test_a_root_that_reads_running_again_at_recheck_makes_the_release_stale():
    env = make_env(c="5000")
    k = _hold_stale_claim(env, "SUCCEEDED")
    flip = lambda _p: env.runs.set("k6", "run_payroll", ROOT, "RUNNING", tenant_id="k6:operator")  # noqa: E731
    out = run(RELEASE_SPEC, REQ, env, on_decide=flip)
    assert out.state == "FAILED"
    assert "changed since the proposal" in out.detail
    assert read_claim(env.platform_store, k)["owner"] == "dead-child"


def test_a_root_that_reads_running_at_apply_releases_nothing(monkeypatch):
    env = make_env(c="5000")
    k = _hold_stale_claim(env, "SUCCEEDED")
    real = env.runs.status
    calls = {"n": 0}

    def status(*a):
        calls["n"] += 1
        return "RUNNING" if calls["n"] >= 3 else real(*a)  # propose, recheck: terminal; apply: not

    monkeypatch.setattr(env.runs, "status", status)
    out = run(RELEASE_SPEC, REQ, env)
    assert out.state == "FAILED"
    assert "RootStillRunning" in out.detail
    assert read_claim(env.platform_store, k)["owner"] == "dead-child"


@pytest.mark.parametrize("case", ["child", "other-tenant"])
def test_run_status_refusals_fail_the_release_closed(case):
    env = make_env(c="5000")
    if case == "child":
        k = _hold_stale_claim(env, None, root="child-1")
        env.runs.set("k6", "run_payroll", "parent-1", "SUCCEEDED", tenant_id="k6:operator")
        env.runs.add_child("k6", "child-1")
    else:
        k = _hold_stale_claim(env, "SUCCEEDED", root_tenant="k6:employer:a")
    with pytest.raises(ValueError, match=r"child proposal|another tenant"):  # fails closed
        run(RELEASE_SPEC, REQ, env)
    assert read_claim(env.platform_store, k)["owner"] == "dead-child"


def test_a_release_whose_claim_moved_after_approval_is_stale():
    env = make_env(c="5000")
    k = _hold_stale_claim(env)
    from awx_k6_payroll.books import release_claim, take_claim

    def moved(_p):
        release_claim(env.platform_store, k, "dead-child")
        take_claim(env.platform_store, k, "live-proposal", env.fake.now())

    out = run(RELEASE_SPEC, REQ, env, on_decide=moved)
    assert out.state == "FAILED"
    assert read_claim(env.platform_store, k)["owner"] == "live-proposal"


def wf_origin_agent():
    from awx_common import Origin

    return Origin.AGENT


# ---------------------------------------------------------------- tamper and resume


def test_a_sent_record_that_changes_after_approval_makes_the_run_stale_with_nothing_sent():
    """Runner version of the re-emission tamper test: the approval binds the in-flight
    conversion's sent record; if that record changes before execution, rule 1's re-run sees a
    different hash and the batch fails closed with nothing re-emitted."""
    env = make_env(a="20000")
    run(LOCK_SPEC, anna(), env, approvers=())
    env.fake.inject("fx.conversions.create", Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=30 * DAY)
    first = children(run(PAYROLL_SPEC, anna(), env))["a"]
    assert first.state != "SUCCEEDED"
    k = keys_for(env.platform_store, first.proposal.intent)
    rid = k.conversion_rid("EUR", "USD")
    sent_before = len(env.money_ops())

    def tamper(_p):
        item = env.platform_store.get(f"sent/{rid}")
        env.platform_store.put(
            item.key,
            {**item.value, "sell": item.value["sell"] + Money.of("1.00", "USD")},
            expected_version=item.version,
        )

    out = run(PAYROLL_SPEC, anna(), env, on_decide=tamper)
    assert out.state == "FAILED"
    assert "changed since the proposal" in out.detail
    assert len(env.money_ops()) == sent_before
    assert sent(env.platform_store, rid)["sell"] != first.proposal.intent["fx_lock"]["EUR"]["sell"]


def test_a_resumed_child_passes_its_own_claim_and_others_stay_blocked():
    """PolicyContext.proposal_id: the claim's owner re-running its policy (a redrive
    or a redelivered recheck) is not blocked by its own claim."""
    from awx_common.workflow import PolicyContext
    from awx_k6_payroll.books import ensure_book, take_claim

    env = make_env(c="5000")
    ensure_book(env.platform_store)
    intent = EMPLOYER_SPEC.validate({"pay_period": PERIOD, **carl()["employers"][0]})
    k = keys_for(env.platform_store, intent)
    take_claim(env.platform_store, k, "child-own", env.fake.now())
    tenant = env.resolver.resolve("k6:employer:c", kit="k6")

    def decide(pid):
        ctx = PolicyContext(
            tenant=tenant,
            now=env.fake.now(),
            read=lambda r: env.fake.read(r.__class__(r.path, r.query, tenant.connected_account_id)),
            store=env.kit_store,
            platform_store=env.platform_store,
            read_platform=env.fake.read,
            proposal_id=pid,
        )
        return EMPLOYER_SPEC.policy(intent, ctx)

    own = decide("child-own")
    assert not any("another run of this period" in r for r in own.reasons)
    other = decide("child-other")
    assert other.risk_tier.value == "BLOCKED"
    assert any("another run of this period" in r for r in other.reasons)


# ---------------------------------------------------------------- kit-bound tenant rows


def test_an_employer_row_of_another_kit_is_refused_and_nothing_is_sent():
    from awx_common.tenancy import TenantRow

    env = make_env(c="5000")
    env.extra_rows["k6:employer:c"] = TenantRow("k7", env.accts["c"], "customer")
    out = run(PAYROLL_SPEC, carl(), env)
    assert out.state == "FAILED"
    assert "TenantScopeViolation" in (out.detail or "")
    assert env.fake.sent_ops == []


def test_the_release_resolves_k6_operator_in_kit_k6_and_refuses_another_kits_operator():
    from awx_common.errors import TenantScopeViolation
    from awx_common.tenancy import TenantRow

    env = make_env(c="5000")
    k = _hold_stale_claim(env, "SUCCEEDED")
    env.extra_rows["k5:pool"] = TenantRow("k5", None, "operator")
    with pytest.raises(TenantScopeViolation):
        env.resolver.resolve("k5:pool", kit="k6")
    other = env.resolver.resolve("k5:pool", kit="k5")
    with pytest.raises(TenantScopeViolation):
        run(RELEASE_SPEC, REQ, env, tenant=other)
    assert read_claim(env.platform_store, k)["owner"] == "dead-child"
    op = env.resolver.resolve("k6:operator", kit="k6")
    assert (op.kit, op.role, op.connected_account_id) == ("k6", "operator", None)
    assert run(RELEASE_SPEC, REQ, env, tenant=op).state == "SUCCEEDED"
    assert read_claim(env.platform_store, k) is None
