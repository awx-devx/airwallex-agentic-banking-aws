"""Kit 6's match checks cover the spine's match_fields, and a foreign
object under a kit request_id is never adopted (no false 'paid', no fee on unpaid payroll)."""

import pytest
from awx_common import Money, Operation
from awx_common.store import InMemoryKitStore
from awx_k6_payroll import PAYROLL_SPEC
from awx_k6_payroll import operations as ops
from awx_k6_payroll.books import Keys
from awx_k6_payroll.model import parse_employer
from awx_k6_payroll.workflow import CONVERSION_MATCH, FEE_MATCH, TRANSFER_MATCH, keys_for
from awx_spine.endpoints import ENDPOINTS
from k6_data import PERIOD, employer, us
from k6_fake import FakeAirwallex, children, make_env, run

BOOK = "0123456789abcdef0123456789abcdef"


def sample_ops():
    k = Keys(BOOK, InMemoryKitStore(), "c", PERIOD)
    c = parse_employer(employer("c", us("carl", "800.00")), PERIOD)["contractors"][0]
    return {
        "transfers.create": (ops.create_transfer(k, c, "ben_1", "0" * 64), TRANSFER_MATCH),
        "fx.conversions.create": (
            ops.create_conversion(
                k.conversion_rid("EUR", "USD"), k, Money.of("10.00", "EUR"), "USD", "q1", "0" * 64
            ),
            CONVERSION_MATCH,
        ),
        "charges.create": (
            ops.create_fee_charge(k, Money.of("12.00", "USD"), "0" * 64),
            FEE_MATCH,
        ),
    }


@pytest.mark.parametrize("name", ["transfers.create", "fx.conversions.create", "charges.create"])
def test_each_kit_list_covers_the_spine_match_fields_its_body_carries(name):
    op, kit_fields = sample_ops()[name]
    body = dict(op.body)
    if name == "charges.create":
        body["source"] = "acct_x"  # set by bind_platform_operation
    spine = {f for f in ENDPOINTS[name].match_fields if f in body}
    assert spine <= set(kit_fields), sorted(spine - set(kit_fields))
    # sell_amount and quote_id are the spine's fields too, so conversions check them all
    if name == "fx.conversions.create":
        assert set(ENDPOINTS[name].match_fields) <= set(kit_fields)


def carl():
    return {"pay_period": PERIOD, "employers": [employer("c", us("carl", "800.00"))]}


def test_a_same_amount_transfer_to_another_beneficiary_is_never_adopted():
    """After carl's beneficiary is created, a transfer of the same 800.00 USD to someone else
    lands under carl's payout request_id. The kit must not read it as carl's payout: nothing
    sent for carl, no fee charged, and BLOCKED for a person, even after the foreign transfer
    is PAID."""
    env = make_env(c="5000")
    acct = env.accts["c"]
    state = {}

    def plant(fake, op):
        if state:
            return
        k = keys_for(env.platform_store, {"employer": "c", "pay_period": PERIOD})
        ben_body = dict(op.body, nickname="someone-else", request_id="poc-i6-foreign-ben-0001")
        ben = Operation(**{**op.__dict__, "body": ben_body, "request_id": ben_body["request_id"]})
        other = FakeAirwallex.execute(fake, ben).provider_id
        rid = k.payout_rid("carl")
        c = parse_employer(employer("c", us("carl", "800.00")), PERIOD)["contractors"][0]
        # Identical to the kit's own payout body except beneficiary_id.
        body = dict(ops.create_transfer(k, c, other, "0" * 64).body)
        foreign = Operation(
            kit="k6",
            name="transfers.create",
            method="POST",
            path="/api/v1/transfers/create",
            body=body,
            request_id=rid,
            business_key="foreign",
            intent_hash="0" * 64,
            on_behalf_of=acct,
        )
        state["tid"] = FakeAirwallex.execute(fake, foreign).provider_id

    env.fake.after_write["beneficiaries.create"] = plant
    first = children(run(PAYROLL_SPEC, carl(), env))["c"]
    assert first.state != "SUCCEEDED"
    assert env.money_ops("transfers.create") == []  # nothing sent for carl
    env.fake.after_write.clear()
    for status in ("SENT", "PAID"):
        env.fake.simulate(
            "transfer_transition", transfer_id=state["tid"], next_status=status, on_behalf_of=acct
        )
    for _ in range(2):
        later = children(run(PAYROLL_SPEC, carl(), env))["c"]
        assert later.state == "BLOCKED"
        assert any("does not match" in r for r in later.proposal.reasons)
    assert env.fake.objects("charges") == []  # no fee on a payroll that did not pay
    assert env.money_ops() == []
    carl_ben = next(
        b["id"] for b in env.fake.objects("beneficiaries") if b["nickname"] != "someone-else"
    )
    assert not [t for t in env.fake.objects("transfers") if t["beneficiary_id"] == carl_ben]


def test_reason_and_reference_are_optional_but_spine_fields_are_required():
    from awx_k6_payroll.workflow import TRANSFER_OPTIONAL, RequestIdBoundElsewhere, _check_match

    want = {"transfer_amount": Money.of("1.00", "USD"), "beneficiary_id": "b1", "reason": "x"}
    fields = ("transfer_amount", "beneficiary_id", "reason")
    _check_match(
        {"transfer_amount": 1, "beneficiary_id": "b1"}, "r", want, fields, TRANSFER_OPTIONAL
    )
    with pytest.raises(RequestIdBoundElsewhere):  # carried and different
        _check_match(
            {"transfer_amount": 1, "beneficiary_id": "b1", "reason": "y"},
            "r",
            want,
            fields,
            TRANSFER_OPTIONAL,
        )
    with pytest.raises(RequestIdBoundElsewhere):  # a spine field missing is a mismatch
        _check_match({"transfer_amount": 1, "reason": "x"}, "r", want, fields, TRANSFER_OPTIONAL)


def test_read_payroll_plan_after_a_paid_payroll_returns_a_plan():
    """The read tool cannot state the fee's source; it reports instead of
    raising, while policy keeps the full check."""
    from awx_common import ReadRequest, bind_read
    from awx_common.workflow import PolicyContext
    from awx_k6_payroll import READS
    from k6_fake import PLATFORM

    env = make_env(c="5000")
    assert children(run(PAYROLL_SPEC, carl(), env))["c"].state == "SUCCEEDED"

    def read_tenant(tenant_id: str, req: ReadRequest):
        return env.fake.read(bind_read(req, env.resolver.resolve(tenant_id, kit="k6")))

    ctx = PolicyContext(
        tenant=PLATFORM,
        now=env.fake.now(),
        read=env.fake.read,
        store=env.platform_store,
        platform_store=env.platform_store,
        read_platform=env.fake.read,
        read_tenant=read_tenant,
    )
    plan = READS["read_payroll_plan"](carl()["employers"][0] | {"pay_period": PERIOD}, ctx)
    assert plan["fee"] == "charge found; source not checked by this read"
    assert plan["already_paid"] == ["carl"]
    assert plan["fundable"] is True
    again = children(run(PAYROLL_SPEC, carl(), env))["c"]
    assert again.state == "SUCCEEDED", again.postcondition  # policy's full check still passes
