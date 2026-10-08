"""The kit 7 acceptance scenario and the live sandbox probes (marker `sandbox`).

The scenario runs against FakeAirwallex: `uv run --all-packages pytest -m sandbox
kits/k7_lending`. `make check` deselects the marker. A live run swaps the fake for the
sandbox through AirwallexClient (see DESIGN.md, "Live sandbox runs").

The live rules are applied already, so a live run only swaps the port:
- the whole run holds `flock /tmp/awx-sandbox.lock`, so two kits never race on the shared
  platform balance;
- borrower connected accounts are created once and found again by their `poc-i7-` label
  (connected accounts cannot be deleted);
- every run opens a book with a fresh id (rule M2), so it can never adopt an earlier run's
  objects;
- at the end every movement this book sent is terminal and no hold is left (kit 7 makes no
  transfers or card authorisations, so those checks belong to the kits that do).
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta

import pytest
from awx_common import ApprovalDecision, Decision
from awx_common.approvals import ApprovalBindingError, approval_state
from awx_k7_lending import SPECS, load_book
from awx_k7_lending.policy import TrancheOutcome

pytestmark = pytest.mark.sandbox

LOCK = "/tmp/awx-sandbox.lock"  # noqa: S108 - the agreed cross-kit sandbox lock
LIVE = os.environ.get("AWX_K7_LIVE") == "1"  # live sandbox probes only; unset offline


@pytest.fixture
def sandbox_lock() -> Iterator[None]:
    with open(LOCK, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def end_state_problems(port, book) -> list[str]:
    """Kit 7 moves money only through charges and connected account transfers, so the end
    state is about this book's own request_ids: every one resolves
    to a terminal object, and no hold is left. Other kits' objects on the shared account are
    not this run's to judge."""
    from awx_k7_lending.provider import PLATFORM_TERMINAL, find_by_request_id

    problems = []
    for rid, (kind, _lid) in sorted(book.issued_request_ids().items()):
        found = find_by_request_id(port.read, kind, rid, book.config.currency)
        posted = any(e.request_id == rid for e in book.ledger.entries) or any(
            r.charge_request_id == rid for t in book.loans() for r in book.week_records(t.loan_id)
        )
        if found is None and rid not in book.released() and not posted:
            problems.append(f"{kind} request_id {rid} sent but never resolved (lost send)")
        if found is not None and found.status not in PLATFORM_TERMINAL:
            problems.append(f"{kind} {found.provider_id} left at {found.status}")
    if book.pending():
        problems.append(f"tranche holds left: {[p.request_id for p in book.pending()]}")
    for t in book.loans():
        if book.pending_collection(t.loan_id) is not None:
            problems.append(f"charge hold left on {t.loan_id}")
    return problems


def test_acceptance_scenario_on_the_sandbox_path(k7, sandbox_lock):
    """Report kit 7 acceptance test, end to end: activate borrowers, fund wallets, collect the
    week's repayments scaled to revenue, decide and execute a disbursement that respects the
    floor, and reconcile the portfolio afterwards."""
    fake = k7.K7Fake(k7.usd(k7.SHARED_WALLET))
    earlier = k7.make_portfolio(fake=fake)  # an earlier run on the same shared account
    w = k7.make_portfolio(fake=fake)
    assert w.borrowers == earlier.borrowers  # accounts found again by label, not recreated
    assert load_book(w.store)[0].book_id != load_book(earlier.store)[0].book_id
    a, b = (w.borrowers[x].connected_account_id for x in ("loan-a", "loan-b"))

    def run(spec, raw, tenant, **kw):
        out = w.run(spec, raw, tenant=tenant, **kw)
        assert out.state in ("SUCCEEDED", "BLOCKED"), out.detail
        return out

    for lid in ("loan-a", "loan-b"):
        assert run(SPECS["disburse_advance"], {"loan_id": lid}, w.borrowers[lid]).post.ok
    fake.move_to(k7.IN_W42)
    fake.deposit(a, k7.usd("10000.00"))
    fake.deposit(b, k7.usd("6000.00"))
    fake.spend(b, k7.usd("10700.00"))  # B under-pays this week
    fake.move_to(k7.AFTER_W42)
    for lid in ("loan-a", "loan-b"):
        out = run(
            SPECS["collect_weekly_repayment"], {"loan_id": lid, "week": k7.W42}, w.borrowers[lid]
        )
        assert out.post.ok, out.post.detail
    book = load_book(w.store)[0]
    assert book.ledger.arrears("loan-b") == k7.usd("600.00")
    c = run(SPECS["disburse_advance"], {"loan_id": "loan-c"}, w.borrowers["loan-c"])
    assert c.decision.computed["outcome"] == TrancheOutcome.PARTIAL
    assert c.post.ok, c.post.detail
    book = load_book(w.store)[0]
    assert book.ledger.platform_cash() >= book.config.reserve_floor

    # Recurring collection replay is rejected and charges nobody (final_report.md kit 7).
    charges = fake.creates("charges.create")
    replay = w.run(
        SPECS["collect_weekly_repayment"],
        {"loan_id": "loan-a", "week": k7.W42},
        tenant=w.borrowers["loan-a"],
        now=k7.AFTER_W42 + timedelta(hours=1),
    )
    assert replay.state == "BLOCKED"
    assert not replay.ops
    assert fake.creates("charges.create") == charges

    # Approval tampering fails closed: a forged hash, an edited intent, an edited amount.
    forged = ApprovalDecision(
        c.proposal.proposal_id, "0" * 64, "approver-1", Decision.APPROVE, c.proposal.created_at
    )
    with pytest.raises(ApprovalBindingError):
        approval_state(c.proposal, [forged], now=c.proposal.created_at)
    for edited in (
        replace(c.proposal, intent={"loan_id": "loan-a"}),
        replace(c.proposal, computed={**c.proposal.computed, "amount": k7.usd("3900.00")}),
    ):
        with pytest.raises(ApprovalBindingError):
            approval_state(edited, k7.approve(c.proposal), now=c.proposal.created_at)

    r = run(SPECS["reconcile_portfolio"], {}, k7.PLATFORM)
    assert r.post.ok, r.post.detail
    fake.advance(timedelta(minutes=1).total_seconds())
    assert end_state_problems(fake, load_book(w.store)[0]) == []


@pytest.mark.skipif(not LIVE, reason="live sandbox probe (AWX_K7_LIVE=1, sandbox client)")
def test_probe_refused_charge_consumes_request_id():  # pragma: no cover - live sandbox only
    """Live: send charges/create with an invalid reason (refused at create), then the same
    request_id with a valid body. Record whether the second gets request_id_duplicate (the
    refusal consumed it) or 201. Kit 7 never reuses a refused request_id either way, so
    the answer only goes into FAKE-FIDELITY.md."""
    raise NotImplementedError("live probe: wire AirwallexClient and a poc-i7- borrower")


@pytest.mark.skipif(not LIVE, reason="live sandbox probe (AWX_K7_LIVE=1, sandbox client)")
def test_probe_balances_history_row_shape():  # pragma: no cover - live sandbox only
    """Live: simulate a deposit and a connected account transfer into a poc-i7- borrower, then
    GET /api/v1/balances/history for that window with x-on-behalf-of. Record the query
    parameters it accepts and each row's fields: transaction_type, amount, currency, and
    whether the transfer's row names it (source_id, request_id, reference or description).
    The revenue filter (provider.is_revenue) relies on those names; the backstop
    (LoanBook.tranche_credits) holds whatever the shape."""
    raise NotImplementedError("live probe: wire AirwallexClient and a poc-i7- borrower")


def test_end_state_reports_an_unresolved_lost_send(k7):
    """A sent request_id with no object, not released and not posted, is a
    lost send the scenario never resolved, and the end-state check names it."""
    w = k7.make_portfolio()
    w.fake.lose_next("connected_account_transfers.create")
    out = w.run(SPECS["disburse_advance"], {"loan_id": "loan-a"}, tenant=w.borrowers["loan-a"])
    rid = out.ops[0].request_id
    problems = end_state_problems(w.fake, load_book(w.store)[0])
    assert any(rid in p and "lost send" in p for p in problems)
