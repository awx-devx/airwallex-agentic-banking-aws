"""Finality allowlist and request-id derivation: unit and property tests.

Transfer reads use the sandbox's post-failure shape (FAKE-FIDELITY "Transfers"): CANCELLED with
only `failure: {code, details: {type}, message}`.
"""

from __future__ import annotations

import pytest
from awx_k3_incident import airwallex as awx
from awx_k3_incident.finality import (
    ESCALATE_STATUSES,
    FAILURE_REASON_CODES,
    KNOWN_STATUSES,
    REPLACEABLE_FAILURE_CODES,
    REPLACEABLE_FAILURE_TYPES,
    WAIT_STATUSES,
    IncidentDecision,
    assess,
    failure_code_of,
)
from hypothesis import given
from hypothesis import strategies as st


def failure_for(ftype):
    """What FakeAirwallex (and the sandbox) reads back after a FAILED bank return."""
    assert ftype == "BENEFICIARY_BANK_RETURNED"
    return {
        "code": "90802",
        "details": {"type": "INCORRECT_ROUTING"},
        "message": "Beneficiary bank returned",
    }


ALL_CODES = sorted(FAILURE_REASON_CODES.values())
BARE_CANCEL = {"details": {"type": "PAYMENT_RECALLED"}, "message": "simulation"}


def failure(code):
    return {"code": code, "details": {"type": "INCORRECT_ROUTING"}, "message": "m"}


def t(status, fail=None, **extra):
    d = {"id": "tfr_1", "status": status, **extra}
    if fail is not None:
        d["failure"] = fail
    return d


statuses = st.one_of(st.sampled_from(sorted(KNOWN_STATUSES)), st.text(max_size=12))
failures = st.one_of(
    st.none(),
    st.just(BARE_CANCEL),
    st.builds(failure, st.one_of(st.sampled_from([*ALL_CODES, "90000"]), st.text(max_size=6))),
    st.dictionaries(st.text(max_size=8), st.text(max_size=8), max_size=3),
)
extras = st.fixed_dictionaries(
    {},
    optional={
        "failure_type": st.one_of(st.sampled_from(sorted(FAILURE_REASON_CODES)), st.text()),
        "failure_reason": st.one_of(st.sampled_from([f"{c}: x" for c in ALL_CODES]), st.text()),
    },
)


@pytest.mark.parametrize("status", sorted(WAIT_STATUSES))
def test_in_flight_statuses_wait(status):
    assert assess(t(status, failure("90802"))).decision is IncidentDecision.WAIT


@pytest.mark.parametrize("status", sorted(ESCALATE_STATUSES))
def test_person_statuses_escalate(status):
    assert assess(t(status)).decision is IncidentDecision.ESCALATE


def test_live_bank_return_read_replaces():
    """The exact read the sandbox returned after FAILED with BENEFICIARY_BANK_RETURNED."""
    read = t("CANCELLED", failure_for("BENEFICIARY_BANK_RETURNED"))
    a = assess(read)
    assert a.decision is IncidentDecision.REPLACE
    assert (a.failure_code, a.failure_type) == ("90802", "BENEFICIARY_BANK_RETURNED")


@pytest.mark.parametrize("code", sorted(REPLACEABLE_FAILURE_CODES))
def test_allowlisted_codes_replace(code):
    assert assess(t("CANCELLED", failure(code))).decision is IncidentDecision.REPLACE


def test_integer_code_is_accepted():
    assert assess(t("CANCELLED", failure(90802))).decision is IncidentDecision.REPLACE


@pytest.mark.parametrize(
    "fail",
    [
        BARE_CANCEL,  # a bare cancellation has no code
        None,
        failure("90701"),  # ACCOUNT_CLOSED, live-proven code, not allowlisted
        failure("99902"),  # OTHER, what a FAILED with no failure_type reads back as
        failure("90000"),  # the fake's code for types without a proven code
        failure("91001"),  # RECALL_REQUESTED
        failure("91401"),  # SYSTEM_ERROR: untestable, late-credit risk
        failure("91402"),  # CHANNEL_TIMEOUT: untestable, late-credit risk
        failure("not-a-code"),
        {"code": None, "message": "x"},
    ],
)
def test_cancelled_without_allowlisted_code_escalates(fail):
    assert assess(t("CANCELLED", fail)).decision is IncidentDecision.ESCALATE


def test_top_level_failure_type_alone_never_replaces():
    """The sandbox never puts failure_type on a read, so a read
    carrying only a top-level failure_type is not a path to REPLACE."""
    read = t("CANCELLED", failure_type="BENEFICIARY_BANK_RETURNED")
    assert assess(read).decision is IncidentDecision.ESCALATE


def test_details_type_is_ignored():
    """details.type is INCORRECT_ROUTING for every failure type,
    so it is never cross-checked against the code."""
    a = assess(t("CANCELLED", failure("90802")))
    assert a.decision is IncidentDecision.REPLACE


def test_codes_that_disagree_escalate():
    read = t("CANCELLED", failure("90802"), failure_reason="90701: Account closed")
    code, doubts = failure_code_of(read)
    assert code is None
    assert doubts
    assert assess(read).decision is IncidentDecision.ESCALATE


@pytest.mark.parametrize("status", ["", "cancelled", "FAILED_FINAL", "DISPATCHED", None])
def test_unknown_status_fails_closed(status):
    assert assess({"id": "x", "status": status}).decision is IncidentDecision.ESCALATE


def test_allowlist_names_match_codes():
    assert {FAILURE_REASON_CODES[n] for n in REPLACEABLE_FAILURE_TYPES} == set(
        REPLACEABLE_FAILURE_CODES
    )


@given(statuses, failures, extras)
def test_property_replace_only_on_cancelled_with_allowlisted_code(status, fail, extra):
    a = assess(t(status, fail, **extra))
    if a.decision is IncidentDecision.REPLACE:
        assert status == "CANCELLED"
        assert a.failure_code in REPLACEABLE_FAILURE_CODES


@given(failures, extras)
def test_property_sent_never_replaces(fail, extra):
    assert assess(t("SENT", fail, **extra)).decision is IncidentDecision.WAIT


@given(st.from_regex(r"\A[A-Za-z0-9_-]{1,64}\Z"), st.from_regex(r"\A[A-Za-z0-9_-]{1,64}\Z"))
def test_property_replacement_request_id_is_per_original(a, b):
    ra, rb = awx.replacement_request_id(a), awx.replacement_request_id(b)
    assert ra == awx.replacement_request_id(a)
    assert 10 <= len(ra) <= 50  # transfers reject request_id outside 10..50 chars (code 018)
    assert (ra == rb) == (a == b)
    assert ra != awx.original_request_id("0123456789abcdef", a)


def test_allowlist_is_only_the_bank_return_code():
    """91401 and 91402 are untestable in the sandbox and a timeout or system
    error can still be followed by a late credit, so only 90802 may be replaced."""
    assert frozenset({"90802"}) == REPLACEABLE_FAILURE_CODES
    assert frozenset({"BENEFICIARY_BANK_RETURNED"}) == REPLACEABLE_FAILURE_TYPES
