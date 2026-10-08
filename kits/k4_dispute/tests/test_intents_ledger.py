"""Intent validation (strict, canonical) and the outcome ledger."""

from __future__ import annotations

import random

import pytest
from awx_common import Money, intent_hash
from awx_k4_dispute import SPECS
from awx_k4_dispute.config import DEFAULT_CONFIG, DisputeConfig
from awx_k4_dispute.intents import IntentError
from awx_k4_dispute.ledger import (
    CLOSE_REASONS_FOR_STATUS,
    EntryKind,
    EntrySource,
    outcome_entries,
    total,
)
from awx_k4_dispute.provider import DisputeStatus, parse_dispute
from hypothesis import given
from hypothesis import strategies as st
from k4_harness import challenge_input, money

CHALLENGE = SPECS["challenge_dispute"]
ACCEPT = SPECS["accept_dispute"]


def accept_raw(**over):
    raw = {
        "dispute_id": "dsp_1",
        "disputed_amount": money("15.00"),
        "reason": "LOW_VALUE",
        "rationale": "small",
    }
    raw.update(over)
    return raw


@pytest.mark.parametrize(
    "over",
    [
        {"request_id": "x"},
        {"account_id": "acct_x"},
        {"approver": "me"},
        {"disputed_amount": {"amount": 15.0, "currency": "AUD"}},
        {"disputed_amount": {"amount": "15.001", "currency": "AUD"}},
        {"disputed_amount": {"amount": "15", "currency": "aud"}},
        {"reason": "ACCEPT_EVERYTHING"},
        {"dispute_id": "../transfers"},
        {"rationale": ""},
        {"rationale": "x" * 1001},
    ],
)
def test_accept_validation_is_strict(over):
    with pytest.raises(IntentError):
        ACCEPT.validate(accept_raw(**over))


def test_policy_version_is_server_set_and_bound():
    intent = ACCEPT.validate(accept_raw())
    assert intent["policy_version"] == DEFAULT_CONFIG.policy_version
    other = SPECS["accept_dispute"].validate(accept_raw())
    assert intent_hash(intent) == intent_hash(other)
    from awx_k4_dispute.workflow import build_specs

    moved = build_specs(DisputeConfig(policy_version="v2"))["accept_dispute"].validate(accept_raw())
    assert intent_hash(moved) != intent_hash(intent)


def test_challenge_takes_only_evidence_ids_from_the_model():
    raw = challenge_input("dsp_1")
    ids = raw["evidence_ids"]
    with pytest.raises(IntentError, match="unique"):
        CHALLENGE.validate(dict(raw, evidence_ids=[ids[0], ids[0]]))
    with pytest.raises(IntentError, match="duplicate"):
        CHALLENGE.validate(dict(raw, facts=[raw["facts"][0], raw["facts"][0]]))
    # Model-supplied metadata is refused outright: size, hash and type come from the index.
    meta = {"evidence_id": ids[0], "sha256": "a" * 64, "content_type": "image/jpeg"}
    with pytest.raises(IntentError):
        CHALLENGE.validate(dict(raw, evidence_ids=[meta]))
    with pytest.raises(IntentError, match="unexpected"):
        CHALLENGE.validate(dict(raw, evidence=[meta]))


@given(seed=st.integers(min_value=0, max_value=10_000))
def test_challenge_intent_hash_ignores_list_order(seed):
    raw = challenge_input("dsp_1")
    rng = random.Random(seed)
    shuffled = dict(
        raw,
        evidence_ids=rng.sample(raw["evidence_ids"], len(raw["evidence_ids"])),
        facts=rng.sample(raw["facts"], len(raw["facts"])),
    )
    assert intent_hash(CHALLENGE.validate(raw)) == intent_hash(CHALLENGE.validate(shuffled))


def view(status: str, amount: str = "900.00"):
    return parse_dispute({"id": "d", "amount": amount, "currency": "AUD", "status": status})


def test_ledger_entries_by_outcome():
    lost = outcome_entries(view("LOST"), DEFAULT_CONFIG)
    assert {(e.kind, e.source) for e in lost} == {
        (EntryKind.DISPUTED_AMOUNT_LOST, EntrySource.PROVIDER),
        (EntryKind.DISPUTE_FEE, EntrySource.MODELLED),
        (EntryKind.HANDLING_COST, EntrySource.MODELLED),
    }
    won = outcome_entries(view("WON"), DEFAULT_CONFIG)
    assert total(won, EntrySource.PROVIDER, "AUD") == Money.zero("AUD")
    assert total(won, EntrySource.MODELLED, "AUD") == Money.of("45.00", "AUD")
    accepted = outcome_entries(view("ACCEPTED"), DEFAULT_CONFIG)
    assert EntryKind.DISPUTE_FEE not in {e.kind for e in accepted}
    with pytest.raises(ValueError, match="not terminal"):
        outcome_entries(view("CHALLENGED"), DEFAULT_CONFIG)


@given(
    minor=st.integers(min_value=1, max_value=10_000_000),
    status=st.sampled_from(["WON", "LOST", "ACCEPTED", "EXPIRED"]),
    fee_on_accept=st.booleans(),
)
def test_provider_and_modelled_totals_never_mix(minor, status, fee_on_accept):
    cfg = DisputeConfig(fee_on_accept=fee_on_accept)
    amount = Money.from_minor(minor, "AUD")
    entries = outcome_entries(view(status, format(amount.amount, "f")), cfg)
    provider = total(entries, EntrySource.PROVIDER, "AUD")
    modelled = total(entries, EntrySource.MODELLED, "AUD")
    assert provider == (Money.zero("AUD") if status == "WON" else amount)
    terms = cfg.terms["AUD"]
    contested = status in ("WON", "LOST")
    expected = (terms.contest_handling_cost if contested else terms.accept_handling_cost) + (
        terms.dispute_fee if contested or fee_on_accept else Money.zero("AUD")
    )
    assert modelled == expected
    assert all(
        e.source is EntrySource.MODELLED
        for e in entries
        if e.kind in (EntryKind.DISPUTE_FEE, EntryKind.HANDLING_COST)
    )


def test_every_terminal_status_has_a_close_reason():
    for status in (
        DisputeStatus.WON,
        DisputeStatus.LOST,
        DisputeStatus.ACCEPTED,
        DisputeStatus.EXPIRED,
    ):
        assert CLOSE_REASONS_FOR_STATUS[status]
