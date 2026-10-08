"""Kit 3 acceptance against the real Airwallex sandbox; the live test is marker
`sandbox`, the cleanup proofs on FakeAirwallex run in `make check`.

The live test runs only with AWX_K3_LIVE=1 (plus `-m sandbox`). It holds
`flock /tmp/awx-sandbox.lock` for the whole run, including cleanup, so no other kit races on the
shared platform balance. The client loads the sandbox key itself; nothing here reads, prints or
logs it.

Each run: a fresh kit store (fresh book id, M2), the kit's reused US LOCAL beneficiary
`poc-i3-supplier` (found by nickname and account number across every page, created once), a
1.00 USD staged original, the kit's own failure simulation (SENT, then FAILED with
BENEFICIARY_BANK_RETURNED), and one approved replacement that the sandbox driver takes to PAID.

Cleanup runs always, under the flock: an original still in flight goes
through the kit's own simulate_transfer_failure (a bank return: refunded, no new money); a
replacement still in flight is driven to PAID with transition_op (it is already debited). Cleanup
never proposes a replacement. Then nothing may be at SENT, or the test fails. The run record
($AWX_K3_SHAPES) is written from `finally` with the outcome, every cleanup step, the lock state
and the replacement's request_id.

`test_cleanup_*` prove the cleanup on FakeAirwallex by failing the scenario after SENT.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from awx_common import Operation, OperationStatus, ReadRequest, derive_request_id
from awx_common.sandbox import make_transfer_advance
from awx_common.workflow import KitWorkflowSpec, PolicyContext
from awx_k3_incident import READS, SPECS, ledger
from awx_k3_incident import airwallex as awx
from awx_k3_incident.workflow import build_specs
from k3_fixtures import BANK_RETURN, PLATFORM, Kit3

LIVE = os.environ.get("AWX_K3_LIVE") == "1"
LOCK = "/tmp/awx-sandbox.lock"  # noqa: S108  shared by every kit's live run
NICKNAME = "poc-i3-supplier"
ACCOUNT_NUMBER = "56999933"
IN_FLIGHT = ("SCHEDULED", "PROCESSING", "SENT")


@contextmanager
def sandbox_lock():
    with open(LOCK, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


class LivePort:
    """AirwallexClient with the few fake controls Kit3 uses: real sleep and wall clock."""

    def __init__(self) -> None:
        from awx_spine import AirwallexClient, EnvFileCredentials

        self.client = AirwallexClient(EnvFileCredentials())
        self.requests: list[tuple[str, str, str | None]] = []

    def execute(self, op: Operation):
        self.requests.append((op.method, op.path, op.on_behalf_of))
        return self.client.execute(op)

    def read(self, req: ReadRequest):
        return self.client.read(req)

    def advance(self, seconds: float) -> None:
        time.sleep(seconds)

    def clock(self) -> float:
        return time.time()


class LiveKit(Kit3):
    def now(self) -> datetime:
        return datetime.now(UTC)

    def transfer(self, transfer_id: str) -> dict[str, Any]:
        return dict(self.fake.read(awx.transfer_read(transfer_id)))

    def replacements_of(self, oid: str) -> list[dict[str, Any]]:
        return [dict(t) for t in awx.find_replacements(self.fake.read, oid)]

    def context(self) -> PolicyContext:
        return PolicyContext(PLATFORM, self.now(), self.fake.read, store=self.store)


# ---- beneficiary (every page, matched on nickname plus account number) ------------------


def find_beneficiary(read: Callable[[ReadRequest], Any]) -> str | None:
    page = 0
    while page < 50:  # 5,000 beneficiaries: far past anything the shared sandbox holds
        body = read(
            ReadRequest("/api/v1/beneficiaries", {"page_num": str(page), "page_size": "100"})
        )
        items = body.get("items") or []
        for b in items:
            bank = (b.get("beneficiary") or {}).get("bank_details") or {}
            if b.get("nickname") == NICKNAME and bank.get("account_number") == ACCOUNT_NUMBER:
                return str(b["id"])
        if not body.get("has_more") or not items:
            return None
        page += 1
    return None


def ensure_beneficiary(port: Any, run: str) -> str:
    found = find_beneficiary(port.read)
    if found is not None:
        return found
    rid = derive_request_id("k3", f"poc-i3-beneficiary:{run}", "create")
    op = Operation(
        kit="k3",
        name="beneficiaries.create",
        method="POST",
        path="/api/v1/beneficiaries/create",
        body={
            "request_id": rid,
            "nickname": NICKNAME,
            "transfer_methods": ["LOCAL"],
            "beneficiary": {
                "type": "BANK_ACCOUNT",
                "entity_type": "COMPANY",
                "company_name": "poc-i3 Supplier Inc",
                "address": {
                    "city": "New York",
                    "country_code": "US",
                    "postcode": "10001",
                    "state": "US-NY",
                    "street_address": "1 Main Street",
                },
                "bank_details": {
                    "account_currency": "USD",
                    "account_name": "poc-i3 Supplier Inc",
                    "account_number": ACCOUNT_NUMBER,
                    "account_routing_type1": "aba",
                    "account_routing_value1": "021000021",
                    "bank_account_category": "Checking",
                    "bank_country_code": "US",
                    "bank_name": "JPMorgan Chase Bank, NA",
                    "local_clearing_system": "ACH",
                },
            },
        },
        request_id=rid,
        business_key=f"poc-i3-beneficiary:{run}",
        intent_hash="b" * 64,
    )
    result = port.execute(op)
    assert result.status is OperationStatus.SUCCEEDED, (result.error_type, result.error_code)
    return str(result.provider_id)


def shape(t: dict[str, Any]) -> dict[str, Any]:
    """Sanitised: field names and types, plus the status and failure values."""
    out: dict[str, Any] = {k: type(v).__name__ for k, v in sorted(t.items())}
    out["status"] = t.get("status")
    out["failure"] = t.get("failure")
    out["created_at"] = t.get("created_at")
    out["updated_at"] = t.get("updated_at")
    return out


# ---- the scenario, its cleanup and its record ---------------------------------------------


def cleanup(kit: Kit3, seen: dict[str, Any]) -> None:
    """Leave nothing in flight. Never proposes a replacement (no kit.replace here)."""
    steps: list[dict[str, Any]] = seen.setdefault("cleanup", [])
    oid = seen.get("original")
    if isinstance(oid, str) and kit.transfer(oid).get("status") in IN_FLIGHT:
        out = kit.run(
            "simulate_transfer_failure", {"transfer_id": oid, "failure_type": BANK_RETURN}
        )
        steps.append(
            {
                "step": "original: simulate_transfer_failure (bank return, refunded)",
                "transfer": oid,
                "state": out.state,
                "after": kit.transfer(oid).get("status"),
            }
        )
        status = kit.transfer(oid).get("status")
        if status in IN_FLIGHT:
            # The kit's run was refused (its stored refusal would be reused under the kit's own
            # simulation request_id): send one direct transition on a cleanup request_id.
            # FAILED is allowed only from SENT; from SCHEDULED/PROCESSING a bare CANCELLED
            # refunds it instead. Neither moves new money.
            if status == "SENT":
                body = {"next_status": "FAILED", "failure_type": BANK_RETURN}
            else:
                body = {"next_status": "CANCELLED"}
            direct = Operation(
                kit=awx.KIT,
                name=awx.TRANSITION_OP,
                method="POST",
                path=f"/api/v1/simulation/transfers/{oid}/transition",
                body=body,
                request_id=derive_request_id(awx.KIT, f"cleanup:{oid}", body["next_status"]),
                business_key=f"cleanup:{oid}",
                intent_hash="f" * 64,
            )
            result = kit.fake.execute(direct)
            steps.append(
                {
                    "step": f"original: direct transition {status} -> {body['next_status']}",
                    "transfer": oid,
                    "result": result.status.value,
                    "after": kit.transfer(oid).get("status"),
                }
            )
    if isinstance(oid, str):
        rep_ids = [str(r["id"]) for r in kit.replacements_of(oid)]
        known = known_replacement_id(kit, seen)
        if not rep_ids and known is not None:
            rep_ids = [known]
        for rid in rep_ids:
            for _ in range(3):  # SCHEDULED/PROCESSING -> SENT -> PAID
                status = kit.transfer(rid).get("status")
                if status not in IN_FLIGHT:
                    break
                target = "PAID" if status == "SENT" else "SENT"
                result = kit.fake.execute(
                    awx.transition_op(transfer_id=rid, next_status=target, intent_hash="f" * 64)
                )
                steps.append(
                    {
                        "step": f"replacement: transition {status} -> {target}",
                        "transfer": rid,
                        "result": result.status.value,
                        "after": kit.transfer(rid).get("status"),
                    }
                )


def known_replacement_id(kit: Kit3, seen: dict[str, Any]) -> str | None:
    """The replacement's id from what this run already holds, never a re-read of the list:
    the create's provider_id if the replace run returned one, else the provider_id the
    operation store recorded under the lock's request_id."""
    if isinstance(seen.get("replacement"), str):
        return seen["replacement"]
    oid = seen.get("original")
    held = ledger.get_lock(kit.store, oid) if isinstance(oid, str) else None
    if held is None:
        return None
    rec = kit.ops.get(held.request_id)
    pid = rec.result.provider_id if rec is not None and rec.result is not None else None
    if pid:
        seen["replacement_from_operation_store"] = True
        return str(pid)
    return None


def record(kit: Kit3, seen: dict[str, Any]) -> None:
    oid = seen.get("original")
    if isinstance(oid, str):
        held = ledger.get_lock(kit.store, oid)
        seen["lock"] = None if held is None else {"state": held.state, "attempt": held.attempt}
        reps = kit.replacements_of(oid)
        known = known_replacement_id(kit, seen)
        if not reps and known is not None:
            # A listing may lag: read the replacement this run saw by id, not the same list.
            try:
                reps = [kit.transfer(known)]
            except Exception as exc:  # recorded, never hidden
                seen["replacement_read_error"] = type(exc).__name__
        seen["replacement_request_ids"] = [str(r.get("request_id")) for r in reps]
        ids = [oid, *[str(r["id"]) for r in reps]]
        seen["end_state"] = {i: kit.transfer(i).get("status") for i in ids}
        stuck = [i for i, st in seen["end_state"].items() if st in IN_FLIGHT]
        if stuck:
            seen["person_action"] = (
                f"NEEDS_PERSON: {stuck} still in flight after cleanup. In the Airwallex sandbox "
                "web app (or awx-sandbox), move each one: an original at SENT to FAILED with "
                "failure_type BENEFICIARY_BANK_RETURNED (refunds it); at SCHEDULED or PROCESSING "
                "to CANCELLED; a replacement to SENT then PAID. Never create a new transfer."
            )
    path = os.environ.get("AWX_K3_SHAPES")
    if path:
        with open(path, "w") as fh:
            json.dump(seen, fh, indent=2, default=str)


def acceptance(
    kit: Kit3,
    seen: dict[str, Any],
    *,
    replace_spec: KitWorkflowSpec | None = None,
    fail_after_sent: bool = False,
) -> None:
    """The kit 3 acceptance scenario. Always cleans up, records, and checks nothing is at SENT."""
    run = seen["run"]
    try:
        try:
            sent = kit.send(f"inv-{run}", amount="1.00", stage=True)
            assert sent.state == "SUCCEEDED", (sent.detail, sent.postcondition)
            oid = str(sent.results["send"][0].provider_id)
            seen["original"] = oid
            seen["book_id"] = ledger.book_id(kit.store)
            original = kit.transfer(oid)
            seen["original_after_create"] = shape(original)
            assert original["reference"].startswith(awx.POC_LABEL)
            assert original["reference"].endswith(awx.NOSETTLE_MARKER)

            # At SENT: WAIT, no lock, nothing created.
            kit.fake.execute(
                awx.transition_op(transfer_id=oid, next_status="SENT", intent_hash="e" * 64)
            )
            if fail_after_sent:
                raise AssertionError("forced failure after SENT (cleanup proof)")
            a = READS["read_incident_assessment"]({"transfer_id": oid}, kit.context())
            assert a["decision"] == "WAIT", a
            assert a["duplicate_lock_held"] is False
            assert kit.replace(oid).state == "BLOCKED"
            assert kit.replacements_of(oid) == []

            # The kit's own simulation: FAILED with a bank return, read back CANCELLED.
            sim = kit.run(
                "simulate_transfer_failure", {"transfer_id": oid, "failure_type": BANK_RETURN}
            )
            assert sim.state == "SUCCEEDED", (sim.detail, sim.postcondition)
            failed = kit.transfer(oid)
            seen["original_after_fail"] = shape(failed)
            assert failed["status"] == "CANCELLED"
            assert str(failed["failure"]["code"]) == "90802"

            out = kit.run(
                "replace_failed_transfer",
                {"original_transfer_id": oid, "incident_ref": "INC-1"},
                spec=replace_spec,
            )
            created = [r.provider_id for r in out.results.get("replace", ()) if r.provider_id]
            if created:  # the id the create returned, so record() can read it by id
                seen["replacement"] = str(created[-1])
            reps = kit.replacements_of(oid)
            seen["replacements"] = [shape(kit.transfer(str(r["id"]))) for r in reps]
            seen["replace_state"] = out.state
            seen["replace_detail"] = out.postcondition.detail if out.postcondition else out.detail
            assert out.state == "SUCCEEDED", (out.detail, out.postcondition)
            (rep,) = reps
            seen["replacement"] = rep["id"]
            assert kit.transfer(str(rep["id"]))["status"] == "PAID"
            assert rep["request_id"] == awx.replacement_request_id(oid, 1)
            assert ledger.get_lock(kit.store, oid).state == ledger.LANDED
            closure = kit.store.get(f"closure:{oid}")
            assert closure is not None
            assert closure.value["original_failure"]["code"] == "90802"
            seen["outcome"] = "passed"
        except BaseException as exc:
            seen["outcome"] = f"failed: {type(exc).__name__}: {str(exc)[:300]}"
            raise
    finally:
        try:
            cleanup(kit, seen)
        finally:
            record(kit, seen)
        left = [i for i, s in (seen.get("end_state") or {}).items() if s in IN_FLIGHT]
        assert not left, f"left in flight after cleanup: {left}"


def test_cleanup_when_the_simulation_is_refused_gets_the_original_off_sent(tmp_path, monkeypatch):
    """The kit's own simulation is refused after SENT; cleanup sends
    one direct FAILED on its own request_id, so the original is refunded."""
    from awx_spine.fake import Fault

    monkeypatch.setenv("AWX_K3_SHAPES", str(tmp_path / "run.json"))
    kit = Kit3.new()
    seen: dict[str, Any] = {"run": "fake-4"}
    real_execute = kit.fake.execute

    def refuse_after_sent(op):
        result = real_execute(op)
        if op.name == awx.TRANSITION_OP and op.body.get("next_status") == "SENT":
            kit.fake.inject("transfers.simulate_transition", Fault.REFUSE)  # the kit's FAILED
        return result

    kit.fake.execute = refuse_after_sent  # type: ignore[method-assign]
    with pytest.raises(AssertionError):
        acceptance(kit, seen)
    oid = seen["original"]
    assert kit.transfer(oid)["status"] == "CANCELLED"
    assert kit.replacements_of(oid) == []
    rec = json.loads((tmp_path / "run.json").read_text())
    assert any(s["step"] == "original: direct transition SENT -> FAILED" for s in rec["cleanup"])
    assert rec["end_state"] == {oid: "CANCELLED"}
    assert "person_action" not in rec


def test_record_tells_a_person_what_to_do_when_the_original_is_stuck(tmp_path, monkeypatch):
    """Every cleanup transition is refused; the record says what a person does."""
    from awx_spine.fake import Fault

    monkeypatch.setenv("AWX_K3_SHAPES", str(tmp_path / "run.json"))
    kit = Kit3.new()
    seen: dict[str, Any] = {"run": "fake-5"}
    real_execute = kit.fake.execute

    def refuse_after_sent(op):
        result = real_execute(op)
        if op.name == awx.TRANSITION_OP and op.body.get("next_status") == "SENT":
            kit.fake.inject("transfers.simulate_transition", Fault.REFUSE, times=5)
        return result

    kit.fake.execute = refuse_after_sent  # type: ignore[method-assign]
    with pytest.raises(AssertionError, match="left in flight"):
        acceptance(kit, seen)
    rec = json.loads((tmp_path / "run.json").read_text())
    assert rec["end_state"] == {seen["original"]: "SENT"}
    assert rec["person_action"].startswith("NEEDS_PERSON:")
    assert "BENEFICIARY_BANK_RETURNED" in rec["person_action"]


@pytest.mark.sandbox
@pytest.mark.skipif(not LIVE, reason="live sandbox run: set AWX_K3_LIVE=1")
def test_acceptance_live():
    seen: dict[str, Any] = {"run": datetime.now(UTC).strftime("%Y%m%d%H%M%S")}
    with sandbox_lock():
        port = LivePort()
        kit = LiveKit(port)  # type: ignore[arg-type]  # fresh kit store: fresh book (M2)
        kit.beneficiary_id = ensure_beneficiary(port, seen["run"])
        seen["beneficiary_id"] = kit.beneficiary_id
        acceptance(kit, seen)


# ---- cleanup proof on FakeAirwallex (no live calls) --------------------------------------


def test_cleanup_after_a_failure_at_sent_refunds_the_original(tmp_path, monkeypatch):
    monkeypatch.setenv("AWX_K3_SHAPES", str(tmp_path / "run.json"))
    kit = Kit3.new()
    seen: dict[str, Any] = {"run": "fake-1"}
    with pytest.raises(AssertionError, match="forced failure after SENT"):
        acceptance(kit, seen, fail_after_sent=True)
    oid = seen["original"]
    assert kit.transfer(oid)["status"] == "CANCELLED"
    assert kit.transfer(oid)["failure"]["code"] == "90802"
    assert kit.replacements_of(oid) == []  # cleanup proposed nothing
    rec = json.loads((tmp_path / "run.json").read_text())
    assert rec["outcome"].startswith("failed: AssertionError")
    assert rec["cleanup"][0]["step"].startswith("original: simulate_transfer_failure")
    assert rec["end_state"] == {oid: "CANCELLED"}


def test_cleanup_drives_a_replacement_left_at_sent_to_paid(tmp_path, monkeypatch):
    """The driver stops at SENT (as a slow sandbox might), the replace step times out and the
    test fails; cleanup takes the one replacement to PAID and records it."""
    monkeypatch.setenv("AWX_K3_SHAPES", str(tmp_path / "run.json"))

    def sent_only(op, result, body, now, attempt):
        adv = make_transfer_advance(awx.KIT)(op, result, body, now, attempt)
        return adv if adv is not None and adv.body["next_status"] == "SENT" else None

    kit = Kit3.new()
    seen: dict[str, Any] = {"run": "fake-2"}
    stuck = build_specs(sent_only)["replace_failed_transfer"]
    with pytest.raises(AssertionError):
        acceptance(kit, seen, replace_spec=stuck)
    oid = seen["original"]
    (rep,) = kit.replacements_of(oid)
    assert rep["status"] == "PAID"
    rec = json.loads((tmp_path / "run.json").read_text())
    assert any("replacement: transition SENT -> PAID" in s["step"] for s in rec["cleanup"])
    assert "SENT" not in rec["end_state"].values()
    assert rec["replacement_request_ids"] == [awx.replacement_request_id(oid, 1)]
    assert rec["lock"]["attempt"] == 1


def test_clean_fake_run_records_outcome_lock_and_request_id(tmp_path, monkeypatch):
    """A clean run's record proves its own end state: outcome, lock and request_id."""
    monkeypatch.setenv("AWX_K3_SHAPES", str(tmp_path / "run.json"))
    kit = Kit3.new()
    seen: dict[str, Any] = {"run": "fake-3"}
    acceptance(kit, seen, replace_spec=SPECS["replace_failed_transfer"])
    rec = json.loads((tmp_path / "run.json").read_text())
    assert rec["outcome"] == "passed"
    assert rec["lock"] == {"state": ledger.LANDED, "attempt": 1}
    assert rec["replacement_request_ids"] == [awx.replacement_request_id(rec["original"], 1)]
    assert rec["cleanup"] == []


class PagedBeneficiaries:
    def __init__(self, pages):
        self.pages = pages
        self.asked: list[str] = []

    def __call__(self, req: ReadRequest):
        n = int(req.query["page_num"])
        self.asked.append(req.query["page_num"])
        return {"items": self.pages[n], "has_more": n + 1 < len(self.pages)}


def ben(i: str, nick: str, acct: str):
    return {"id": i, "nickname": nick, "beneficiary": {"bank_details": {"account_number": acct}}}


def test_find_beneficiary_pages_and_matches_nickname_and_account():
    """The beneficiary search reads every page and matches nickname and account number."""
    reader = PagedBeneficiaries(
        [
            [ben("a", "other", ACCOUNT_NUMBER)] * 100,
            [ben("b", NICKNAME, "11111111"), ben("c", NICKNAME, ACCOUNT_NUMBER)],
        ]
    )
    assert find_beneficiary(reader) == "c"
    assert reader.asked == ["0", "1"]
    assert find_beneficiary(PagedBeneficiaries([[ben("b", NICKNAME, "1")]])) is None


def test_record_reads_the_replacement_by_id_when_the_listing_lags(tmp_path, monkeypatch):
    """When the listing lags, the fallback is a read by id of the object the create
    returned, never the same list again."""
    monkeypatch.setenv("AWX_K3_SHAPES", str(tmp_path / "run.json"))

    class ReadKit(Kit3):  # reads as LiveKit does, on the fake clock
        transfer = LiveKit.transfer
        replacements_of = LiveKit.replacements_of

    base = Kit3.new()
    kit = ReadKit(base.fake, beneficiary_id=base.beneficiary_id)
    seen: dict[str, Any] = {"run": "fake-6"}
    acceptance(kit, seen, replace_spec=SPECS["replace_failed_transfer"])
    oid, rid = seen["original"], seen["replacement"]
    reads: list[str] = []
    real_read = kit.fake.read

    def no_listing(req):
        reads.append(req.path)
        if req.path == "/api/v1/transfers":
            return {"items": []}  # the listing lags
        return real_read(req)

    kit.fake.read = no_listing  # type: ignore[method-assign]
    record(kit, seen)
    rec = json.loads((tmp_path / "run.json").read_text())
    assert rec["end_state"] == {oid: "CANCELLED", rid: "PAID"}
    assert f"/api/v1/transfers/{rid}" in reads


def test_the_replacement_is_found_through_the_operation_store_when_the_run_raised():
    """The replace run raised before `seen` got the id, and the
    listing lags; the operation store's record for the lock's request_id still names it."""
    kit = Kit3.new()
    oid = kit.failed_original()
    assert (
        kit.run(
            "replace_failed_transfer",
            {"original_transfer_id": oid, "incident_ref": "INC-1"},
            spec=build_specs(None)["replace_failed_transfer"],
            sandbox=False,
        ).state
        == "FAILED"
    )  # stays PROCESSING; finality times out
    seen: dict[str, Any] = {"run": "fake-7", "original": oid}
    (rep,) = kit.replacements_of(oid)
    assert known_replacement_id(kit, seen) == rep["id"]
    assert seen["replacement_from_operation_store"] is True
    cleanup(kit, seen)
    assert kit.transfer(rep["id"])["status"] == "PAID"
