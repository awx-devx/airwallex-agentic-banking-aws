"""Property, through the real runner against FakeAirwallex: no sequence of incidents,
timeouts, invisible landings, list lag, refusals, rate limits, clock jumps and replacement
failures ever yields two replacements for one original, and a replacement only ever exists for
an original that really was CANCELLED with the allowlisted failure code."""

from __future__ import annotations

from awx_common.errors import AwxError
from awx_k3_incident.finality import REPLACEABLE_FAILURE_CODES, failure_code_of
from awx_spine.fake import Fault
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from k3_fixtures import BANK_RETURN, Kit3

DAY = 86400

events = st.lists(
    st.one_of(
        st.tuples(st.just("propose"), st.sampled_from(["INC-1", "INC-2"])),
        st.tuples(st.just("propose"), st.sampled_from(["INC-3", "INC-4"])),
        st.tuples(st.just("close"), st.none()),
        st.tuples(st.just("sent"), st.none()),
        st.tuples(st.just("fail"), st.sampled_from([BANK_RETURN, "ACCOUNT_CLOSED", None])),
        st.tuples(st.just("timeout"), st.sampled_from([5, 600, 8 * DAY])),
        st.tuples(st.just("lag"), st.sampled_from([5, 60])),
        st.tuples(st.just("refuse"), st.none()),
        st.tuples(st.just("rate_limit"), st.none()),
        st.tuples(st.just("clock"), st.sampled_from([30, 3600, 3 * DAY])),
        st.tuples(st.just("fail_replacement"), st.none()),
    ),
    max_size=10,
)


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.booleans(), events)
def test_property_at_most_one_replacement_and_never_while_in_flight(failed_first, seq):
    kit = Kit3.new()
    oid = kit.staged_original()
    if failed_first:  # bias toward the interesting half: a replaceable original
        kit.fail(oid, BANK_RETURN)
    for kind, arg in seq:
        if kind == "sent" and kit.transfer(oid)["status"] in ("SCHEDULED", "PROCESSING"):
            kit.fake.simulate("transfer_transition", transfer_id=oid, next_status="SENT")
        elif kind == "fail" and kit.transfer(oid)["status"] in ("SCHEDULED", "PROCESSING", "SENT"):
            kit.fail(oid, arg)
        elif kind == "timeout":
            kit.fake.inject("transfers.create", Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=arg)
        elif kind == "lag":
            kit.fake.inject("transfers.create", Fault.LIST_LAG, visible_after_s=arg)
        elif kind == "refuse":
            kit.fake.inject("transfers.create", Fault.INSUFFICIENT_FUNDS)
        elif kind == "rate_limit":
            kit.fake.inject("transfers.create", Fault.RATE_LIMIT)
        elif kind == "clock":
            kit.advance(arg)
        elif kind == "close":
            kit.run("close_incident", {"original_transfer_id": oid})
        elif kind == "fail_replacement":
            for rep in kit.replacements_of(oid):
                if rep["status"] in ("SCHEDULED", "PROCESSING", "SENT"):
                    try:
                        kit.fail(rep["id"], "ACCOUNT_CLOSED")
                    except AwxError:
                        pass  # in the fake, a transfer hidden by list lag cannot be simulated yet
        elif kind == "propose":
            before = len(kit.replacements_of(oid))
            truth = kit.transfer(oid)
            kit.replace(oid, arg)
            if len(kit.replacements_of(oid)) > before:
                assert truth["status"] == "CANCELLED"
                assert failure_code_of(truth)[0] in REPLACEABLE_FAILURE_CODES
        # fake.objects includes transfers no read can see yet
        assert len(kit.replacements_of(oid)) <= 1


@settings(max_examples=15, deadline=None)
@given(st.lists(st.sampled_from(["INC-1", "INC-2", "INC-3"]), min_size=1, max_size=4))
def test_property_sent_never_yields_a_replacement(refs):
    kit = Kit3.new()
    oid = kit.staged_original()
    kit.fake.simulate("transfer_transition", transfer_id=oid, next_status="SENT")
    for ref in refs:
        assert kit.replace(oid, ref).state == "BLOCKED"
    assert kit.replacements_of(oid) == []
