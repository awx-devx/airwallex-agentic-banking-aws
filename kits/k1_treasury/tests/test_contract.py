"""The kit's exports: SPECS, TOOLS and READS line up; reads stay inside the allowlist; ledger
rules; packaging and transfer labels."""

from __future__ import annotations

from datetime import UTC, datetime

import awx_k1_treasury
import pytest
from awx_common import SERVER_OWNED_FIELDS, ToolKind
from awx_common.workflow import KitWorkflowSpec
from awx_k1_treasury import READS, SPECS, TOOLS
from awx_k1_treasury.book import load_book_id
from awx_k1_treasury.intake import InvalidInput
from hypothesis import given
from hypothesis import strategies as st

NOW = datetime(2026, 10, 19, tzinfo=UTC)
OFFICE = {"obligation_ids": ["ob-office-usd"]}


def test_every_propose_tool_has_a_spec_and_every_read_tool_a_read():
    propose = {t.name for t in TOOLS if t.kind is ToolKind.PROPOSE}
    assert propose == {"propose_lock_rate", "propose_execute_plan", "propose_release_obligation"}
    reads = {t.name for t in TOOLS if t.kind is ToolKind.READ}
    assert propose == {s.tool_name for s in SPECS.values()}
    assert reads == set(READS)
    for action, spec in SPECS.items():
        assert isinstance(spec, KitWorkflowSpec)
        assert spec.action == action
        assert spec.kit == "k1"
    assert set(awx_k1_treasury.__all__) == {"SPECS", "TOOLS", "READS", "OPERATOR_COMMANDS"}


def _props(schema):
    names = set(schema.get("properties", {}))
    for sub in schema.get("properties", {}).values():
        names |= _props(sub)
        if isinstance(sub.get("items"), dict):
            names |= _props(sub["items"])
    return names


def test_no_tool_takes_an_amount_account_or_request_id():
    for t in TOOLS:
        names = _props(t.input_schema)
        assert not names & SERVER_OWNED_FIELDS
        assert not {n for n in names if "amount" in n or "rate" in n or "beneficiary" in n}


@pytest.mark.parametrize("name", sorted(READS))
def test_read_tools_work_and_stay_in_the_allowlist(env, name):
    args = {"buy_currency": "EUR"} if name == "read_fx_rate" else {}
    out = READS[name](args, env.ctx())  # env.ctx() asserts every path against the allowlist
    assert isinstance(out, dict)
    assert env.port.executed == []  # reads never write


def test_read_tools_reject_unexpected_input(env):
    with pytest.raises(InvalidInput):
        READS["read_balances"]({"tenant_id": "x"}, env.ctx())
    with pytest.raises(InvalidInput):
        READS["read_fx_rate"]({"buy_currency": "USD"}, env.ctx())


def test_preview_with_contradiction_shows_the_higher_tier(env, contradiction):
    plain = READS["read_treasury_plan"]({}, env.ctx())
    flagged = READS["read_treasury_plan"]({"contradictions": [contradiction]}, env.ctx())
    assert plain["confidence_band"] == "HIGH"
    assert flagged["confidence_band"] == "LOW"
    assert plain["conversions"] == flagged["conversions"]


def test_every_emitted_operation_passes_the_tenant_scope_check(env):
    """Kit 1 runs as the platform tenant, with no on_behalf_of and no body-bound
    endpoint, so every operation it emits (sandbox advances included) passes."""
    from awx_common import TenantContext
    from awx_common.tenancy import check_tenant_scope

    env.lock({"obligation_ids": ["ob-supplier-eur", "ob-shipping"]})
    out = env.execute({"obligation_ids": ["ob-supplier-eur", "ob-shipping"]})
    assert out.state == "SUCCEEDED", out.detail
    names = set(env.port.names())
    assert {
        "fx.quotes.create",
        "fx.conversions.create",
        "transfers.create",
        "transfers.simulate_transition",
    } <= names
    for o in env.port.executed:
        assert o.on_behalf_of is None
        check_tenant_scope(o, TenantContext("platform"))


def test_locks_are_owned_and_idempotent():
    from awx_common.store import InMemoryKitStore
    from awx_k1_treasury.locks import (
        LockState,
        ObligationLocked,
        acquire,
        read_lock,
        set_state,
        undo_acquire,
    )

    store = InMemoryKitStore()
    assert acquire(store, "ob-a", "1", "p-1").attempt == 1
    assert acquire(store, "ob-a", "1", "p-1").attempt == 1  # re-entry
    assert not acquire(store, "ob-a", "1", "p-1").fresh
    with pytest.raises(ObligationLocked):
        acquire(store, "ob-a", "1", "p-2")
    with pytest.raises(ObligationLocked):  # a new book version does not bypass the lock
        acquire(store, "ob-a", "2", "p-2")
    with pytest.raises(ObligationLocked):  # nor does the same owner naming another version
        acquire(store, "ob-a", "2", "p-1")
    assert not set_state(store, "ob-a", "p-2", LockState.RELEASED)
    assert set_state(store, "ob-a", "p-1", LockState.SUBMITTED, now=NOW)
    assert read_lock(store, "ob-a").submitted_at == NOW
    assert not set_state(
        store, "ob-a", "p-1", LockState.RELEASED, only_from=frozenset({LockState.LOCKED})
    )
    assert set_state(store, "ob-a", "p-1", LockState.RELEASED)
    assert acquire(store, "ob-a", "2", "p-2").attempt == 2  # released: v2, next attempt
    assert read_lock(store, "ob-a").obligation_version == "2"
    assert set_state(store, "ob-a", "p-2", LockState.PAID)
    assert not set_state(store, "ob-a", "p-2", LockState.RELEASED)  # PAID is final
    assert read_lock(store, "ob-a").state is LockState.PAID
    with pytest.raises(ObligationLocked):
        acquire(store, "ob-a", "3", "p-3")
    # undo restores the previous attempt, so an undone take consumes nothing
    got = acquire(store, "ob-b", "1", "p-4")
    undo_acquire(store, "ob-b", "p-4", got)
    assert read_lock(store, "ob-b").state is LockState.RELEASED
    assert acquire(store, "ob-b", "1", "p-5").attempt == 1


@given(st.lists(st.tuples(st.integers(0, 4), st.integers(0, 4)), max_size=30))
def test_each_quote_books_at_most_one_request(attempts):
    from awx_common.store import InMemoryKitStore
    from awx_k1_treasury.locks import QuoteAlreadyBurned, burn_quote, quote_owner

    store = InMemoryKitStore()
    owners: dict[str, str] = {}
    for q, r in attempts:
        qid, rid = f"q{q}", f"r{r}"
        try:
            burn_quote(store, qid, rid)
        except QuoteAlreadyBurned:
            assert owners[qid] != rid
            continue
        owners.setdefault(qid, rid)
        assert owners[qid] == rid == quote_owner(store, qid)


def test_submission_record_refuses_a_different_body():
    from awx_common.store import InMemoryKitStore
    from awx_k1_treasury.locks import SubmissionConflict, record_submission

    store = InMemoryKitStore()
    record_submission(store, "r1", "transfer", {"a": 1})
    record_submission(store, "r1", "transfer", {"a": 1})
    with pytest.raises(SubmissionConflict):
        record_submission(store, "r1", "transfer", {"a": 2})


# --- packaging and labels -------------------------------------------------------------------------


def test_kit_depends_on_awx_common_only_and_uses_the_common_driver():
    import tomllib
    from pathlib import Path

    from awx_common.ops import DEDUP_RESEND_MARGIN
    from awx_k1_treasury.policy import PolicyConfig

    meta = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert meta["project"]["dependencies"] == ["awx-common"]
    assert PolicyConfig().dedup_window == DEDUP_RESEND_MARGIN
    src = (Path(__file__).parents[1] / "src/awx_k1_treasury/workflow.py").read_text()
    assert "from awx_common.sandbox import make_transfer_advance" in src
    assert not (Path(__file__).parents[1] / "src/awx_k1_treasury/sandbox.py").exists()


def test_transfer_reference_carries_the_poc_i1_label_and_the_book(env):
    out = env.execute(OFFICE)
    (t,) = env.transfers()
    book8 = load_book_id(env.store)[:8]
    assert t["reference"] == f"poc-i1-{book8}-ob-office-usd"
    assert out.state == "SUCCEEDED"
