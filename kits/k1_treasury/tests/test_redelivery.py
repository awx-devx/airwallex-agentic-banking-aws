"""Redelivery and duplicate objects: every build twice for one proposal (rule 0), and an
older object under the run's request_id is never judged as the run's own."""

from __future__ import annotations

from dataclasses import replace

import pytest
from awx_common import OperationStatus
from awx_k1_treasury.book import load_book_id
from awx_k1_treasury.keys import transfer_body, transfer_request_id
from awx_k1_treasury.locks import LockState, read_lock
from awx_spine.fake import Fault

SUPPLIER = {"obligation_ids": ["ob-supplier-eur"]}
EPOCH_NOW = __import__("awx_spine.fake", fromlist=["EPOCH"]).EPOCH
OFFICE = {"obligation_ids": ["ob-office-usd"]}


def lock(env, oid):
    return read_lock(env.store, oid)


def _twice(spec, step_name):
    steps = []
    for s in spec.steps:
        if s.name == step_name:
            inner = s.build

            def build(ctx, inner=inner):
                inner(ctx)  # the first delivery's result is lost
                return inner(ctx)

            s = replace(s, build=build)
        steps.append(s)
    return replace(spec, steps=tuple(steps))


# --- rule 0: every build, twice for one proposal, first result thrown away ------------------------


@pytest.mark.parametrize(
    ("action", "step", "raw"),
    [
        ("lock_rate", "quote", SUPPLIER),
        ("execute_plan", "convert", SUPPLIER),
        ("execute_plan", "transfer", SUPPLIER),
        ("execute_plan", "transfer", OFFICE),
    ],
)
def test_rule0_each_build_twice_sends_once(env, action, step, raw):
    if action == "execute_plan" and raw is SUPPLIER:
        env.lock(SUPPLIER)
    env.specs[action] = _twice(env.specs[action], step)
    out = env.run(action, raw)
    assert out.state == "SUCCEEDED", out.detail
    assert out.postcondition is not None
    assert out.postcondition.ok
    names = env.port.names()
    if action == "lock_rate":
        assert names.count("fx.quotes.create") == 1
        return
    assert names.count("transfers.create") == 1
    assert len(env.transfers()) == 1
    assert len(env.conversions()) == (1 if raw is SUPPLIER else 0)
    for oid in raw["obligation_ids"]:
        assert lock(env, oid).state is LockState.PAID  # nothing stranded


def test_rule0_release_apply_twice_releases_once(env):
    env.fake.inject("transfers.create", Fault.INSUFFICIENT_FUNDS)
    env.pay(SUPPLIER)
    assert lock(env, "ob-supplier-eur").state is LockState.NEEDS_PERSON
    env.specs["release_obligation"] = _twice(env.specs["release_obligation"], "apply")
    rel = env.release("ob-supplier-eur", "transfer refused at create", approvers=["a1"])
    assert rel.state == "SUCCEEDED", rel.detail
    assert rel.postcondition.ok
    assert lock(env, "ob-supplier-eur").state is LockState.RELEASED
    assert env.port.names().count("transfers.create") == 1


# --- two objects under one request_id -------------------------------------------------------------


def test_two_objects_under_the_request_id_through_the_runner(env):
    from awx_common import Operation

    book_id = load_book_id(env.store)
    rid = transfer_request_id(book_id, "ob-office-usd", 1)
    ob = env.book.obligation("ob-office-usd")
    body = transfer_body(
        {
            "obligation_id": ob.obligation_id,
            "beneficiary_id": ob.beneficiary_id,
            "amount": ob.amount,
            "method": ob.method.value,
        },
        rid,
        book_id,
    )
    op = Operation(
        kit="k1",
        name="transfers.create",
        method="POST",
        path="/api/v1/transfers/create",
        body=body,
        request_id=rid,
        business_key="x",
        intent_hash="b" * 64,
    )
    assert env.fake.execute(op).status is OperationStatus.SUCCEEDED
    env.advance(7 * 86400 + 1)
    assert env.fake.execute(op).status is OperationStatus.SUCCEEDED
    out = env.execute(OFFICE)
    assert out.state == "FAILED"
    assert len(env.transfers()) == 2  # nothing more was sent
    assert lock(env, "ob-office-usd").state is LockState.NEEDS_PERSON
    assert env.execute(OFFICE).state == "BLOCKED"


def test_select_with_a_provider_id_never_returns_another_row():
    from awx_common import OperationResult
    from awx_k1_treasury.api import make_select

    select = make_select("id")
    mine = OperationResult("r" * 12, OperationStatus.SUCCEEDED, 201, "X", "SCHEDULED")
    only_y = {"items": [{"id": "Y", "request_id": "r" * 12, "status": "PAID"}]}
    assert select(only_y, mine) is None  # lagging: not visible yet, never Y
    both = {"items": [*only_y["items"], {"id": "X", "request_id": "r" * 12, "status": "SENT"}]}
    assert select(both, mine)["id"] == "X"
    no_id = OperationResult("r" * 12, OperationStatus.SUCCEEDED, 201, None, None)
    assert select(only_y, no_id)["id"] == "Y"  # the single-row rule, only without an id


def test_older_object_under_the_request_id_with_list_lag_is_never_paid(env):
    """An object Y holds the office transfer's request_id, the dedupe expires, and the run's
    own create lags 900 s. select never judges Y, so the driver never pays Y and the lock goes
    to NEEDS_PERSON instead of PAID while the run's own transfer is unpaid."""
    from awx_common import Operation

    book_id = load_book_id(env.store)
    rid = transfer_request_id(book_id, "ob-office-usd", 1)
    ob = env.book.obligation("ob-office-usd")
    body = transfer_body(
        {
            "obligation_id": ob.obligation_id,
            "beneficiary_id": ob.beneficiary_id,
            "amount": ob.amount,
            "method": ob.method.value,
        },
        rid,
        book_id,
    )
    y = env.fake.execute(
        Operation(
            kit="k1",
            name="transfers.create",
            method="POST",
            path="/api/v1/transfers/create",
            body=body,
            request_id=rid,
            business_key="x",
            intent_hash="b" * 64,
        )
    )
    assert y.status is OperationStatus.SUCCEEDED
    env.advance(7 * 86400 + 1)
    env.fake.inject("transfers.create", Fault.LIST_LAG, visible_after_s=900)
    out = env.execute(OFFICE)
    assert out.state != "SUCCEEDED"
    assert lock(env, "ob-office-usd").state is LockState.NEEDS_PERSON
    statuses = {t["id"]: t["status"] for t in env.transfers()}
    assert statuses[y.provider_id] != "PAID"  # the driver never advanced Y
    assert env.execute(OFFICE).state == "BLOCKED"
