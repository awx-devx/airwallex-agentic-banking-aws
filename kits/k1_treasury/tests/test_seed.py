"""The operator seed step for a deployed kit 1."""

from __future__ import annotations

import pytest
from awx_common.store import InMemoryKitStore
from awx_k1_treasury.book import load_book, load_book_id
from awx_k1_treasury.reads import READS
from awx_k1_treasury.seed import SeedRefused, build_seed_book, seed_deployed
from awx_spine.fake import FakeAirwallex


def test_seed_writes_the_book_and_mints_the_book_id_from_real_beneficiaries(env):
    fake = env.fake
    ben = env.book.obligation("ob-supplier-eur").beneficiary_id  # nickname poc-i1-ben-...
    store = InMemoryKitStore()
    out = seed_deployed(store, {"ob-supplier-eur": ben}, book_version="demo-1", read=fake.read)
    book = load_book(store)
    assert [o.obligation_id for o in book.obligations] == ["ob-supplier-eur"]
    assert book.obligation("ob-supplier-eur").beneficiary_id == ben
    assert out["book_id"] == load_book_id(store)
    assert out["book_id"]
    with pytest.raises(SeedRefused, match="already seeded"):
        seed_deployed(store, {"ob-supplier-eur": ben}, book_version="demo-2")
    seed_deployed(store, {"ob-supplier-eur": ben}, book_version="demo-2", replace_existing=True)
    assert load_book_id(store) == out["book_id"]  # M2: minted once, kept across reseeds


def test_seed_refuses_placeholders_unknown_obligations_and_unlabelled_beneficiaries():
    with pytest.raises(SeedRefused, match="placeholder"):
        build_seed_book({"ob-shipping": "ben-poc-k1-freight-us"}, book_version="v")
    with pytest.raises(SeedRefused, match="no such obligations"):
        build_seed_book({"ob-nope": "x"}, book_version="v")
    with pytest.raises(SeedRefused, match="at least one"):
        build_seed_book({}, book_version="v")
    fake = FakeAirwallex()
    ben = fake.execute(_unlabelled(fake)).provider_id
    with pytest.raises(SeedRefused, match="not a poc-i1- beneficiary"):
        seed_deployed(InMemoryKitStore(), {"ob-shipping": ben}, book_version="v", read=fake.read)


def _unlabelled(fake):
    from awx_common import Operation, derive_request_id

    rid = derive_request_id("k1", "other-ben", "create")
    return Operation(
        kit="k1",
        name="beneficiaries.create",
        method="POST",
        path="/api/v1/beneficiaries/create",
        body={
            "request_id": rid,
            "nickname": "someone-else",
            "transfer_methods": ["LOCAL"],
            "beneficiary": {
                "type": "BANK_ACCOUNT",
                "entity_type": "COMPANY",
                "address": {"country_code": "US", "state": "US-NY"},
                "bank_details": {
                    "bank_account_category": "Checking",
                    "account_routing_type1": "aba",
                },
            },
        },
        request_id=rid,
        business_key="other-ben",
        intent_hash="b" * 64,
    )


def test_after_seeding_the_reads_answer(env):
    from datetime import UTC, datetime

    from awx_common import TenantContext
    from awx_common.workflow import PolicyContext

    store = InMemoryKitStore()
    ctx = PolicyContext(TenantContext("platform"), datetime.now(UTC), env.port.read, store=store)
    assert READS["read_balances"]({}, ctx)["status"] == "NOT_CONFIGURED"
    ben = env.book.obligation("ob-office-usd").beneficiary_id
    seed_deployed(store, {"ob-office-usd": ben}, book_version="demo-1")
    assert "balances" in READS["read_balances"]({}, ctx)


# ---- read failures and open locks ----


@pytest.mark.parametrize("exc", [RuntimeError("probe"), ConnectionError("reset"), KeyError("x")])
def test_any_read_failure_is_a_refusal_and_writes_nothing(env, exc):
    ben = env.book.obligation("ob-supplier-eur").beneficiary_id
    store = InMemoryKitStore()

    def read(req):
        raise exc

    with pytest.raises(SeedRefused, match=type(exc).__name__):
        seed_deployed(store, {"ob-supplier-eur": ben}, book_version="demo-1", read=read)
    assert load_book(store) is None


def test_the_report_lists_open_locks_and_flags_any_a_replace_drops(env):
    from awx_k1_treasury.locks import LockState, acquire, set_state

    sup = env.book.obligation("ob-supplier-eur").beneficiary_id
    off = env.book.obligation("ob-office-usd").beneficiary_id
    store = InMemoryKitStore()
    first = seed_deployed(
        store,
        {"ob-supplier-eur": sup, "ob-office-usd": off},
        book_version="demo-1",
        read=env.fake.read,
    )
    assert first["open_locks"] == {}
    assert first["dropped_with_open_lock"] == []
    acquire(store, "ob-office-usd", "1", "p-run")
    set_state(store, "ob-office-usd", "p-run", LockState.NEEDS_PERSON, "timed out")
    acquire(store, "ob-supplier-eur", "1", "p-run")
    set_state(store, "ob-supplier-eur", "p-run", LockState.PAID)
    out = seed_deployed(
        store,
        {"ob-supplier-eur": sup},
        book_version="demo-2",
        read=env.fake.read,
        replace_existing=True,
    )
    assert out["open_locks"] == {"ob-office-usd": "NEEDS_PERSON"}  # PAID is not listed
    (flag,) = out["dropped_with_open_lock"]
    assert flag.startswith("ob-office-usd has a NEEDS_PERSON lock and is no longer in the book")


def test_open_locks_scans_every_lock_even_after_two_replaces(env):
    """A NEEDS_PERSON lock dropped by an earlier replace is still
    listed by every later report; only the replace that drops it flags it."""
    from awx_k1_treasury.locks import LockState, acquire, set_state

    sup = env.book.obligation("ob-supplier-eur").beneficiary_id
    off = env.book.obligation("ob-office-usd").beneficiary_id
    store = InMemoryKitStore()
    seed_deployed(
        store,
        {"ob-supplier-eur": sup, "ob-office-usd": off},
        book_version="demo-1",
        read=env.fake.read,
    )
    acquire(store, "ob-office-usd", "1", "p-run")
    set_state(store, "ob-office-usd", "p-run", LockState.NEEDS_PERSON, "timed out")
    first = seed_deployed(
        store,
        {"ob-supplier-eur": sup},
        book_version="demo-2",
        read=env.fake.read,
        replace_existing=True,
    )
    assert len(first["dropped_with_open_lock"]) == 1
    second = seed_deployed(
        store,
        {"ob-supplier-eur": sup},
        book_version="demo-3",
        read=env.fake.read,
        replace_existing=True,
    )
    assert second["open_locks"] == {"ob-office-usd": "NEEDS_PERSON"}
    assert second["dropped_with_open_lock"] == []  # dropped earlier, not by this replace
