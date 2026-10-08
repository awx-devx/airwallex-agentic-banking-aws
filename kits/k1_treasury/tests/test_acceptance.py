"""Kit 1 acceptance scenario, marked `sandbox`.

Run once with supported forecast evidence and once with a contradictory email. The same
conversion changes approval tier, the full fee-inclusive plan stays above the reserve floor,
and replaying cannot create a second conversion or transfer.

Without AWX_LIVE it runs against FakeAirwallex (`make sandbox-test`, or `pytest -m sandbox`).
With AWX_LIVE=1 it runs against the real sandbox through the same local_runner:
- it holds `flock /tmp/awx-sandbox.lock` for the whole run, so two kits never race on the
  shared platform balances;
- every object carries a `poc-i1-` label; beneficiaries are found by nickname and reused;
- a fresh book id per run (M2), so request_ids never collide with an earlier run;
- it ends with nothing at SENT (the sandbox driver takes each transfer to PAID) and no
  pending authorisation (kit 1 creates none).
The live variant reads the sandbox key through `EnvFileCredentials` and calls the sandbox
through `AirwallexClient` directly. Set AWX_K1_SHAPES to a path (for example
kits/k1_treasury/evidence/live-run-1.json) to write the sanitised evidence of the run there; a
failed run writes it too, with `outcome: failed` and the error. Each failure message carries the
run's full detail, postcondition included. The live run asserts that every wallet ends at or
above its floor and that the payout is PAID; it does not assert the fee the sandbox charged.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from awx_common import RiskTier, TenantContext
from awx_k1_treasury.locks import LockState, read_lock

pytestmark = pytest.mark.sandbox

LIVE = os.environ.get("AWX_LIVE") == "1"
LOCK_PATH = "/tmp/awx-sandbox.lock"  # noqa: S108 - the agreed cross-kit lock file


@contextmanager
def sandbox_lock() -> Iterator[None]:
    with open(LOCK_PATH, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _run_scenario(env, supported_env, contradiction) -> None:
    supplier = {"obligation_ids": ["ob-supplier-eur"]}
    contradicted = {**supplier, "contradictions": [contradiction]}

    # Run once with supported evidence, end to end: AUTO, no approver.
    supported_env.lock(supplier)
    ok = supported_env.execute(supplier)
    assert ok.state == "SUCCEEDED", (ok.detail, ok.postcondition)
    assert ok.proposal.risk_tier is RiskTier.AUTO

    # Run once with the contradicting email: the same conversion needs a person.
    env.lock(supplier)
    out = env.execute(contradicted, approvers=["approver"])
    assert out.state == "SUCCEEDED", (out.detail, out.postcondition)
    assert out.proposal.risk_tier is RiskTier.SINGLE
    strip = [
        {k: v for k, v in c.items() if k not in ("quote_id", "quote_valid_to", "sell")}
        for c in out.proposal.computed["conversions"]
    ]
    assert strip == [
        {k: v for k, v in c.items() if k not in ("quote_id", "quote_valid_to", "sell")}
        for c in ok.proposal.computed["conversions"]
    ]  # the same conversion
    for e, run in ((env, out), (supported_env, ok)):
        floors = run.proposal.computed["floors"]
        for cur in ("EUR", "USD"):
            assert e.money(cur) >= floors[cur]  # the whole fee-inclusive plan kept the floor

    # Replay 1: the same approved proposal (same proposal_id) is delivered again: its steps
    # re-enter the lock, find it PAID and send nothing.
    from awx_common.workflow import PolicyContext, PolicyDecision, StepContext
    from awx_k1_treasury.locks import ObligationLocked

    p = out.proposal
    decision = PolicyDecision(p.risk_tier, p.reasons, p.computed)
    for step in env.specs["execute_plan"].steps:
        ctx = StepContext(
            p.intent,
            decision,
            PolicyContext(TenantContext("platform"), env.now(), env.ctx().read, store=env.store),
            p.intent_hash,
            dict(out.results),
            p.proposal_id,
        )
        with pytest.raises(ObligationLocked):
            step.build(ctx)
    # Replay 2: a new proposal for the same obligation (other evidence, fresh lock) is blocked.
    assert env.pay(supplier).state == "BLOCKED"
    mine = [c for c in env.conversions() if c.get("request_id") in _rids(out, "convert")]
    assert len(mine) == 1
    assert sum(1 for t in env.transfers() if t.get("request_id") in _rids(out, "transfer")) == 1
    assert read_lock(env.store, "ob-supplier-eur").state is LockState.PAID
    assert not [t for t in env.transfers() if t["status"] == "SENT"]


def _rids(out, step: str) -> set[str]:
    return {r.request_id for r in out.results.get(step, ())}


@pytest.mark.skipif(LIVE, reason="AWX_LIVE=1 runs the live variant")
def test_kit1_acceptance_against_the_fake(make_env, contradiction):
    _run_scenario(make_env(), make_env(), contradiction)


# --- the live run --------------------------------------------------------------------------

LIVE_NICKNAME = "poc-i1-supplier-de"
LIVE_BENEFICIARY = {
    "beneficiary": {
        "entity_type": "COMPANY",
        "company_name": "poc-i1 Supplier GmbH",
        "address": {
            "street_address": "Teststrasse 1",
            "city": "Berlin",
            "postcode": "10115",
            "country_code": "DE",
        },
        "bank_details": {
            "account_name": "poc-i1 Supplier GmbH",
            "account_currency": "EUR",
            "bank_country_code": "DE",
            "iban": "DE89370400440532013000",  # the standard synthetic test IBAN
            "swift_code": "COBADEFFXXX",
        },
    },
    "transfer_methods": ["SWIFT"],
}


def _shape(obj) -> dict:
    """Sanitised: field names and value types, plus statuses and amounts (no ids, no names)."""
    out = {k: type(v).__name__ for k, v in sorted(obj.items())}
    for k in ("status", "usage", "validity", "fee_amount", "transfer_method", "failure"):
        if k in obj:
            out[k] = obj[k]
    return out


class LiveEnv:
    """The real sandbox through AirwallexClient (the client loads the key itself; nothing here
    reads, prints or logs it), with real time, a fresh kit store and the real local_runner."""

    def __init__(self, client, beneficiary_id: str, contradiction) -> None:
        from awx_common.sandbox import make_transfer_advance
        from awx_common.store import InMemoryKitStore
        from awx_k1_treasury.book import seed_book
        from awx_k1_treasury.workflow import build_specs
        from awx_spine.store import InMemoryOperationStore

        self.client = client
        self.ops = InMemoryOperationStore()
        self.store = InMemoryKitStore()
        self.specs = build_specs(transfer_advance=make_transfer_advance("k1"))
        self.sent: list = []
        seed_book(self.store, self._book(beneficiary_id))  # fresh random book id (M2)

    def now(self):
        from datetime import UTC, datetime

        return datetime.now(UTC)

    def balances(self):
        from awx_common import Money, ReadRequest

        rows = self.client.read(ReadRequest("/api/v1/balances/current")).get("items", [])
        return {
            r["currency"]: Money.of(r["available_amount"], r["currency"])
            for r in rows
            if r["currency"] in ("USD", "EUR")
        }

    def _book(self, beneficiary_id: str):
        from datetime import UTC, datetime, timedelta

        from awx_common import Money
        from awx_k1_treasury.book import (
            Forecast,
            Obligation,
            Priority,
            TransferMethod,
            TreasuryBook,
        )

        bal = self.balances()
        due = datetime.now(UTC) + timedelta(days=1)
        return TreasuryBook(
            book_version="live-i1",
            funding_currency="USD",
            # EUR floor = today's EUR balance, so the supplier needs a conversion (fee
            # included); USD floor leaves wide headroom (the sandbox wallet holds ~10M USD).
            floors={"USD": Money.of("9000000", "USD"), "EUR": bal["EUR"]},
            obligations=(
                Obligation(
                    "ob-supplier-eur",
                    "poc-i1 component supplier (DE, SWIFT)",
                    beneficiary_id,
                    Money.of("10.00", "EUR"),
                    TransferMethod.SWIFT,
                    due,
                    Priority.TIME_SENSITIVE,
                ),
            ),
            forecasts=(
                Forecast("rcpt-acme", "Acme Retail", Money.of("8000", "USD"), due, True, True),
            ),
        )

    class _Port:
        def __init__(self, outer) -> None:
            self.outer = outer

        def execute(self, op):
            self.outer.sent.append(op)
            return self.outer.client.execute(op)

        def read(self, req):
            return self.outer.client.read(req)

    def run(self, action, raw, approvers=()):
        import time

        from awx_common import ApprovalDecision, Decision
        from awx_spine_aws.local_runner import run_kit_workflow

        def decide(p):
            return [
                ApprovalDecision(p.proposal_id, p.intent_hash, a, Decision.APPROVE, self.now())
                for a in approvers
            ]

        return run_kit_workflow(
            self.specs[action],
            raw,
            proposed_by="requester",
            tenant=TenantContext("platform"),
            port=self._Port(self),
            store=self.ops,
            decide=decide,
            now=self.now,
            sleep=time.sleep,
            kit_store=self.store,
            platform_store=self.store,
        )


def _live_beneficiary(client) -> str:
    from awx_common import Operation, OperationStatus, ReadRequest, derive_request_id

    listed = client.read(ReadRequest("/api/v1/beneficiaries", {"page_size": "100"}))
    for b in listed.get("items") or []:  # the sandbox ignores ?nickname=; match it here
        if b.get("nickname") == LIVE_NICKNAME:
            return str(b["id"])
    rid = derive_request_id("k1", "poc-i1-beneficiary-de", "create")
    op = Operation(
        kit="k1",
        name="beneficiaries.create",
        method="POST",
        path="/api/v1/beneficiaries/create",
        body={**LIVE_BENEFICIARY, "request_id": rid, "nickname": LIVE_NICKNAME},
        request_id=rid,
        business_key="poc-i1-beneficiary-de",
        intent_hash="b" * 64,
    )
    res = client.execute(op)
    assert res.status is OperationStatus.SUCCEEDED, (res.error_type, res.error_code)
    return str(res.provider_id)


@pytest.mark.skipif(not LIVE, reason="set AWX_LIVE=1 to run against the sandbox")
def test_kit1_acceptance_against_the_sandbox(
    contradiction, explain, evidence_file
):  # pragma: no cover - live only
    from awx_common import ReadRequest
    from awx_common.workflow import PolicyContext, PolicyDecision, StepContext
    from awx_k1_treasury.locks import ObligationLocked
    from awx_spine import AirwallexClient, EnvFileCredentials

    supplier = {"obligation_ids": ["ob-supplier-eur"]}
    seen: dict = {"runs": []}
    with evidence_file(seen, os.environ.get("AWX_K1_SHAPES")), sandbox_lock():
        client = AirwallexClient(EnvFileCredentials())
        ben = _live_beneficiary(client)
        for label, raw, approvers, tier in (
            ("supported", supplier, (), RiskTier.AUTO),
            (
                "contradicted",
                {**supplier, "contradictions": [contradiction]},
                ("approver",),
                RiskTier.SINGLE,
            ),
        ):
            env = LiveEnv(client, ben, contradiction)
            floors = env.store.get("book/current").value["floors"]
            lock_out = env.run("lock_rate", supplier)
            assert lock_out.state == "SUCCEEDED", explain(
                {
                    "label": label,
                    "lock_rate": lock_out.state,
                    "detail": lock_out.detail,
                    "post": lock_out.postcondition,
                }
            )
            out = env.run("execute_plan", raw, approvers=approvers)
            run = {
                "label": label,
                "state": out.state,
                "tier": out.proposal.risk_tier.value,
                "detail": out.detail,
                "post": out.postcondition.detail if out.postcondition else None,
                "conversion": out.proposal.computed.get("conversions"),
            }
            seen["runs"].append(run)
            assert out.state == "SUCCEEDED", explain(run)
            assert out.proposal.risk_tier is tier, explain(run)
            after = env.balances()
            run["end_balances"] = {cur: after[cur] for cur in ("EUR", "USD")}
            run["floors"] = {cur: floors[cur] for cur in ("EUR", "USD")}
            for cur in ("EUR", "USD"):
                assert after[cur] >= floors[cur], explain(run)  # the floor held, fee included
            conv_rid = out.results["convert"][0].request_id
            tr_rid = out.results["transfer"][0].request_id
            convs = client.read(ReadRequest("/api/v1/fx/conversions", {"request_id": conv_rid}))
            trs = client.read(ReadRequest("/api/v1/transfers", {"request_id": tr_rid}))
            assert len(convs["items"]) == 1, explain(run)
            assert len(trs["items"]) == 1, explain(run)
            run["transfer_status"] = trs["items"][0]["status"]
            run["fee_charged"] = trs["items"][0].get("fee_amount")
            assert trs["items"][0]["status"] == "PAID", explain(run)  # nothing left at SENT
            quote = out.results.get("quote") or lock_out.results["quote"]
            run["shapes"] = {
                "quote": _shape(quote[0].body or {}),
                "conversion_list_row": _shape(convs["items"][0]),
                "transfer_list_row": _shape(trs["items"][0]),
            }
            run["objects"] = {
                "quote": quote[0].provider_id or (quote[0].body or {}).get("quote_id"),
                "conversion": convs["items"][0].get("conversion_id"),
                "conversion_request_id": conv_rid,
                "transfer": trs["items"][0].get("id"),
                "transfer_request_id": tr_rid,
                "book_id": env.store.get("book/id").value["book_id"],
            }
            # Replay: the same approved proposal re-delivered sends nothing...
            p = out.proposal
            d = PolicyDecision(p.risk_tier, p.reasons, p.computed)
            for step in env.specs["execute_plan"].steps:
                ctx = StepContext(
                    p.intent,
                    d,
                    PolicyContext(
                        TenantContext("platform"), env.now(), client.read, store=env.store
                    ),
                    p.intent_hash,
                    dict(out.results),
                    p.proposal_id,
                )
                with pytest.raises(ObligationLocked):
                    step.build(ctx)
            # ...and a new proposal for the same obligation is blocked.
            sends = len(env.sent)
            replay = env.run("execute_plan", supplier)
            assert replay.state == "BLOCKED", explain(run)
            assert len(env.sent) == sends, explain(run)
            end = read_lock(env.store, "ob-supplier-eur")
            assert end.state is LockState.PAID
            # The evidence, recorded in the shapes file as well as asserted.
            run["replay_new_proposal"] = replay.state
            run["replay_same_proposal"] = "ObligationLocked at every step"
            run["sends"] = {
                name: sum(1 for o in env.sent if o.name == name)
                for name in sorted({o.name for o in env.sent})
            }
            run["lock_end"] = {
                "state": end.state.value,
                "attempt": end.attempt,
                "claimed_rids": list(end.claimed_rids),
            }
            run["transfer_reference"] = trs["items"][0].get("reference")
        seen["beneficiary"] = ben
        # The release cross-check's paged read, live.
        from datetime import UTC, datetime, timedelta

        since = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S+0000")
        page = client.read(
            ReadRequest(
                "/api/v1/transfers", {"from_created_at": since, "page_num": "0", "page_size": "100"}
            )
        )
        seen["transfers_paged_read"] = {
            "from_created_at": since,
            "accepted": True,
            "has_more": page.get("has_more"),
            "has_more_type": type(page.get("has_more")).__name__,
            "rows": len(page.get("items") or []),
            "poc_i1_rows": sum(
                1
                for t in page.get("items") or []
                if str(t.get("reference", "")).startswith("poc-i1-")
            ),
        }
        a, b = seen["runs"]
        strip = ("quote_id", "quote_valid_to", "sell")
        assert [{k: v for k, v in c.items() if k not in strip} for c in a["conversion"]] == [
            {k: v for k, v in c.items() if k not in strip} for c in b["conversion"]
        ], explain(seen["runs"])  # the same conversion changed tier
