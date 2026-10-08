"""Stage kit 3's original supplier transfer in the sandbox, ready for the deployed agent.

Runs `STAGING_SPECS["send_supplier_transfer"]` locally through
awx_spine_aws.local_runner.run_kit_workflow with Origin.OPERATOR and `stage_for_incident: true`,
so the transfer's reference carries the `-nosettle` marker and the sandbox driver leaves it in
flight. Staging is SINGLE: the script shows the proposal (payee name, bank, account last 4,
amount, fee) and asks the operator to approve it, and records that decision under a second
identity. Any answer but `yes` rejects it and nothing is sent.

    uv run --all-packages python kits/k3_incident/scripts/stage_original_transfer.py \\
        [--beneficiary-id <poc-i3- beneficiary id>] [--amount 1.00] [--yes]

With no --beneficiary-id it uses the `poc-i3-supplier` beneficiary that the kit 3 live test
creates. The sandbox key is loaded by the client and never printed. The script holds
`flock /tmp/awx-sandbox.lock` for the whole run. Its operation store and kit store are in
memory: the deployed tools read the original from Airwallex by its id and need neither. A re-run
is not idempotent, even with the same --payment-key: each run mints a new book, and the
request_id carries the book, so the same key sends a new transfer. Before the send the script
prints the payment key and the transfer's reference (to stderr), so after a crash a person can
find the transfer in the sandbox by that reference before running it again.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import sys
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

LOCK = "/tmp/awx-sandbox.lock"  # noqa: S108  shared by every kit's live run
NICKNAME = "poc-i3-supplier"


@contextmanager
def sandbox_lock(path: str) -> Iterator[None]:
    with open(path, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def find_beneficiary(read: Callable[[Any], Mapping[str, Any]]) -> str | None:
    """The `poc-i3-supplier` beneficiary's id, across every page, or None."""
    from awx_common import ReadRequest

    for page in range(50):
        body = read(
            ReadRequest("/api/v1/beneficiaries", {"page_num": str(page), "page_size": "100"})
        )
        items = body.get("items") or []
        for b in items:
            if b.get("nickname") == NICKNAME:
                return str(b["id"])
        if not body.get("has_more") or not items:
            return None
    return None


def stage(
    port: Any,
    *,
    beneficiary_id: str,
    amount: str,
    payment_key: str,
    confirm: Callable[[str], bool],
    now: Callable[[], datetime],
    sleep: Callable[[float], None],
    proposer: str = "operator-staging",
    approver: str = "operator-approver",
) -> dict[str, Any]:
    """Run the staging workflow once and report what it did."""
    from awx_common import ApprovalDecision, Decision, Proposal, TenantContext
    from awx_common.approvals import Origin
    from awx_common.store import InMemoryKitStore
    from awx_k3_incident import airwallex as awx
    from awx_k3_incident.workflow import STAGING_SPECS
    from awx_spine.store import InMemoryOperationStore
    from awx_spine_aws.local_runner import run_kit_workflow

    def decide(p: Proposal) -> Sequence[ApprovalDecision]:
        # Printed before the send, so a person can find the transfer after a crash.
        print(
            f"payment_key {payment_key}; transfer reference {p.computed.get('reference')}",
            file=sys.stderr,
            flush=True,
        )
        shown = json.dumps(
            {
                "tier": p.risk_tier.value,
                "reasons": list(p.reasons),
                "computed": p.computed,
            },
            indent=2,
            default=str,
        )
        verdict = Decision.APPROVE if confirm(shown) else Decision.REJECT
        return [ApprovalDecision(p.proposal_id, p.intent_hash, approver, verdict, now())]

    out = run_kit_workflow(
        STAGING_SPECS["send_supplier_transfer"],
        {
            "payment_key": payment_key,
            "beneficiary_id": beneficiary_id,
            "amount": amount,
            "currency": "USD",
            "transfer_method": "LOCAL",
            "stage_for_incident": True,
        },
        proposed_by=proposer,
        tenant=TenantContext("platform"),
        port=port,
        store=InMemoryOperationStore(),
        decide=decide,
        now=now,
        sleep=sleep,
        kit_store=InMemoryKitStore(),
        origin=Origin.OPERATOR,
    )
    sent = (out.results or {}).get("send") or ()
    transfer_id = sent[0].provider_id if sent else None
    report: dict[str, Any] = {
        "state": out.state,
        "tier": out.proposal.risk_tier.value,
        "payment_key": payment_key,
        "transfer_id": transfer_id,
        "detail": out.postcondition.detail if out.postcondition else out.detail,
    }
    if out.state != "SUCCEEDED":
        report["reasons"] = list(out.proposal.reasons)
    if transfer_id:
        transfer = port.read(awx.transfer_read(str(transfer_id)))
        report["status"] = transfer.get("status")
        report["reference"] = transfer.get("reference")
        report["next"] = (
            f"tell the deployed agent the supplier has not been paid for transfer {transfer_id}"
        )
    return report


def _ask(shown: str) -> bool:
    print(shown)
    return input("Approve this staging transfer? Type yes: ").strip() == "yes"


def main(
    argv: list[str] | None = None,
    *,
    port: Any = None,
    confirm: Callable[[str], bool] | None = None,
    now: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Stage kit 3's original transfer (sandbox).")
    parser.add_argument("--beneficiary-id", help=f"default: the {NICKNAME} beneficiary")
    parser.add_argument("--amount", default="1.00", help="USD, at most 10.00 (default 1.00)")
    parser.add_argument(
        "--payment-key",
        help="default: a fresh inv-<timestamp>-<hex> key; a re-run with the same key still "
        "sends a new transfer",
    )
    parser.add_argument("--yes", action="store_true", help="approve without asking")
    parser.add_argument("--lock", default=LOCK, help=f"sandbox lock file (default {LOCK})")
    args = parser.parse_args(argv)

    if port is None:
        from awx_spine import AirwallexClient, EnvFileCredentials

        port = AirwallexClient(EnvFileCredentials())
    clock = now or (lambda: datetime.now(UTC))
    key = args.payment_key or f"inv-{clock():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6]}"
    ask = (lambda _shown: True) if args.yes else (confirm or _ask)
    with sandbox_lock(args.lock):
        ben = args.beneficiary_id or find_beneficiary(port.read)
        if ben is None:
            print(
                f"no {NICKNAME} beneficiary in this sandbox: run the kit 3 live test once (it "
                "creates it) or pass --beneficiary-id for a beneficiary whose nickname starts "
                "poc-i3-",
                file=sys.stderr,
            )
            return 2
        report = stage(
            port,
            beneficiary_id=ben,
            amount=args.amount,
            payment_key=key,
            confirm=ask,
            now=clock,
            sleep=sleep or time.sleep,
        )
    print(json.dumps(report, indent=2, default=str))
    return 0 if report["state"] == "SUCCEEDED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
