"""resolve_in_flight_money: the operator tool for money stuck in flight past the
deduplication window.

Kit rule M1 keeps a payout or recovery charge whose outcome is unknown on its original
request_id. Inside the window the kit resends the identical operation or reconciles it. Past
the window (DEDUP_WINDOW, 6 days of the ~7 Airwallex deduplicates) a resend could create a
second object, so the kit sends nothing and a person must decide. This action records that
decision. It never sends an Airwallex write.

The person states what Airwallex says happened, with a reference to that evidence (for
example a support case id):

- landed: the payout reached the seller, or the charge was collected. It is posted as
  SETTLED.
- not_landed: nothing moved. A payout becomes payable and is re-paid inside a later cycle
  under that cycle's request_id. A charge attempt becomes FAILED and leaves the receivable
  owed, so a new attempt (a new request_id) may follow.

Guards:
- The item must be in flight and past the window.
- A read-back by request_id must still find nothing. If Airwallex shows the object,
  reconcile_seller_money records it instead.
- The decision always needs two approvers (DUAL), because it overrides the kit's own
  evidence rule.
- The build repeats every check, so a decision that has gone stale (the object appeared, or
  another run resolved it) fails closed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from awx_common import RiskTier, canonical_json
from awx_common.errors import AwxError, StateConflict, ValidationFailed
from awx_common.store import KitStore
from awx_common.workflow import (
    ApproverScope,
    KitWorkflowSpec,
    PolicyContext,
    PolicyDecision,
    PostconditionResult,
    ReconcileUndetermined,
    StepContext,
    StepSpec,
)

from . import store as st
from . import watch as wt
from .common import (
    ADOPTION_WAIT,
    APPROVAL_TTL,
    account_or_block,
    blocked,
    identity_problem,
)
from .operations import (
    KIT,
    PLATFORM_READS,
    TENANT_READS,
    Outcome,
    build_payout_op,
    build_recovery_op,
    find_by_request_id,
    within_dedup_window,
)
from .recover import apply_charge_outcome
from .settle import apply_payout_outcome

KINDS = ("payout", "charge")
RESOLUTIONS = ("landed", "not_landed")
_REF = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_EVIDENCE = re.compile(r"^[A-Za-z0-9_.:/-]{3,80}$")


def validate_resolve(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValidationFailed("tool input must be an object")
    expected = {"kind", "ref", "resolution", "evidence_ref"}
    if set(raw) != expected:
        raise ValidationFailed(f"need exactly the fields {sorted(expected)}")
    if raw["kind"] not in KINDS:
        raise ValidationFailed("kind must be payout or charge")
    if raw["resolution"] not in RESOLUTIONS:
        raise ValidationFailed("resolution must be landed or not_landed")
    if not isinstance(raw["ref"], str) or not _REF.match(raw["ref"]):
        raise ValidationFailed("ref is missing or malformed")
    if raw["kind"] == "charge" and not raw["ref"].isdigit():
        raise ValidationFailed("a charge ref is its attempt number")
    if not isinstance(raw["evidence_ref"], str) or not _EVIDENCE.match(raw["evidence_ref"]):
        raise ValidationFailed("evidence_ref is missing or malformed")
    return {k: raw[k] for k in sorted(expected)}


def _item(kstore: KitStore, ctx: PolicyContext, intent: Mapping[str, Any]) -> dict[str, Any]:
    """The in-flight item the intent names, with its read-back. Raises StateConflict when it
    is not an item a person may resolve."""
    seller, kind, ref = ctx.tenant.tenant_id, intent["kind"], intent["ref"]
    book = st.book_id(kstore)
    currency = st.profile(kstore).currency
    state = st.ledger_state(kstore, seller, currency)
    if kind == "payout":
        settlement = st.settlement(state, ref)
        if settlement is None or settlement.payout.is_zero():
            raise StateConflict(f"no payout for cycle {ref}")
        if st.payout_outcome(state, ref) is not None:
            raise StateConflict(f"the payout for {ref} already has an outcome")
        amount = settlement.payout
        op = build_payout_op(settlement, ctx.tenant, book_id=book, intent_hash="0" * 64)
    else:
        found = [a for a in st.attempts_in(state) if a.attempt == int(ref)]
        if not found or found[0].outcome not in st.PENDING_RECOVERY:
            raise StateConflict(f"recovery attempt {ref} is not in flight")
        amount = found[0].amount
        op = build_recovery_op(int(ref), amount, ctx.tenant, book_id=book, intent_hash="0" * 64)
    sent = st.first_sent_at(kstore, kind, ref)
    if sent is None:
        raise StateConflict(f"{kind} {ref} was never sent; resend or reconcile it")
    if within_dedup_window(sent, ctx.now):
        raise StateConflict(
            f"{kind} {ref} is still inside the deduplication window; resend or reconcile it"
        )
    if ctx.read_platform is None:
        raise StateConflict("no platform read to check Airwallex first")
    try:
        readback = "empty" if find_by_request_id(op, ctx.read_platform) is None else "found"
    except ReconcileUndetermined as exc:
        readback = f"undetermined: {exc}"[:120]
    if readback == "found":
        raise StateConflict(
            f"Airwallex shows {kind} {ref}; reconcile_seller_money records it, no person needed"
        )
    if readback != "empty":  # only a clean empty read-back
        raise StateConflict(
            f"Airwallex shows something under request_id {op.request_id} ({readback}); "
            "reconcile it or fix it at Airwallex first"
        )
    if (rec := st.resolution(kstore, kind, ref)) is not None:
        # A decision is already recorded and its run died before the ledger move (the
        # outcome checks above passed, so the move is missing). Only the same decision may
        # proceed, and only its owner proposal's build finishes it (rule 0, M4).
        if rec["resolution"] != intent["resolution"]:
            raise StateConflict(
                f"{kind} {ref} already has a {rec['resolution']} decision recorded by proposal "
                f"{rec['owner_proposal_id']}"
            )
        # Adopt it only once its owner can no longer run. Measured from the latest
        # owner change, so a live adopter is never
        # pre-empted by a second adopter.
        since = max(rec["decided_at"], rec.get("adopted_at") or rec["decided_at"])
        free_at = since + ADOPTION_WAIT
        if ctx.now < free_at:
            raise StateConflict(
                f"{kind} {ref} was decided by proposal {rec['owner_proposal_id']}, whose run "
                f"may still finish it; a new proposal may adopt it after {free_at:%Y-%m-%d %H:%M}Z"
            )
        adopts = rec["owner_proposal_id"]
    else:
        adopts = None
    probe = {
        "kind": kind,
        "ref": ref,
        "request_id": op.request_id,
        "amount": amount,
        "first_sent_at": sent,
    }
    if why := wt.counterparty_match(kstore, ctx.tenant, ctx.read_platform, probe, ctx.now):
        raise StateConflict(f"{why}; reconcile it or fix it at Airwallex first")
    attention = kstore.get(f"attention/{kind}/{ref}")
    return {
        "kind": kind,
        "ref": ref,
        "request_id": op.request_id,
        "amount": amount,
        "first_sent_at": sent,
        "readback": readback,
        "stuck_because": (
            attention.value["reason"] if attention is not None else "no reply since the first send"
        ),
        "resolution": intent["resolution"],
        "evidence_ref": intent["evidence_ref"],
        "adopts": adopts,
    }


def make_resolve() -> KitWorkflowSpec:
    def policy_fn(intent: Mapping[str, Any], ctx: PolicyContext) -> PolicyDecision:
        if (bad := account_or_block(ctx)) is not None:
            return bad
        try:
            item = _item(ctx.store, ctx, intent)
        except (AwxError, ValueError) as exc:
            return blocked(f"cannot resolve: {exc}")
        verb = {"landed": "posted as SETTLED", "not_landed": "treated as never moved"}
        reason = (
            f"{item['kind']} {item['ref']} ({item['amount']}, request_id {item['request_id']}) "
            f"has been in flight since {item['first_sent_at']:%Y-%m-%d %H:%M}Z, past the "
            f"deduplication window ({item['stuck_because']}); read-back {item['readback']}. "
            "It will be "
            f"{verb[item['resolution']]} on evidence {item['evidence_ref']}. Sends nothing."
        )
        if item["adopts"]:
            reason += (
                f" Adopts the decision recorded by proposal {item['adopts']}, whose run "
                "died before its ledger move and can no longer run."
            )
        return PolicyDecision(RiskTier.DUAL, (reason,), item)

    def _apply(kstore: KitStore, tenant, rec: Mapping[str, Any]) -> None:
        """The ledger move for a recorded decision, once (apply_* are idempotent)."""
        outcome = Outcome.SETTLED if rec["resolution"] == "landed" else Outcome.FAILED
        if wt.decision_applied(kstore, rec) == "missing":
            if rec["kind"] == "payout":
                state = st.ledger_state(kstore, tenant.tenant_id, rec["amount"].currency)
                apply_payout_outcome(kstore, tenant, st.settlement(state, rec["ref"]), outcome)
            else:
                apply_charge_outcome(kstore, tenant, int(rec["ref"]), rec["amount"], outcome)
        st.clear_attention(kstore, rec["kind"], rec["ref"])

    def build(ctx: StepContext) -> tuple:
        kstore = st.kit_store(ctx.policy)
        tenant = ctx.policy.tenant
        kind, ref = ctx.intent["kind"], ctx.intent["ref"]
        rec = st.resolution(kstore, kind, ref)
        if rec is not None:
            # Rule 0: a retried build of this proposal finishes what
            # its first attempt started (including a crash between the record and the ledger
            # move) and returns. Another proposal's record is never touched.
            if rec["owner_proposal_id"] != ctx.proposal_id:
                adopts = ctx.decision.computed.get("adopts")
                if adopts != rec["owner_proposal_id"]:
                    raise StateConflict(
                        f"{kind} {ref} was decided by proposal {rec['owner_proposal_id']}"
                    )
                item = _item(kstore, ctx.policy, ctx.intent)  # re-check: still empty, free
                if canonical_json(item) != canonical_json(ctx.decision.computed):
                    raise StateConflict("the in-flight item changed since approval")
                st.adopt_resolution(kstore, kind, ref, adopts, ctx.proposal_id, ctx.policy.now)
                rec = st.resolution(kstore, kind, ref)
            _apply(kstore, tenant, rec)
            return ()
        computed = ctx.decision.computed
        item = _item(kstore, ctx.policy, ctx.intent)
        # The approval binds to the item as read at policy time. A changed read-back (for
        # example from empty to undetermined) is a different decision. Refuse it.
        if canonical_json(item) != canonical_json(computed):
            raise StateConflict("the in-flight item changed since approval; propose again")
        landed = item["resolution"] == "landed"
        # The record, and with it the watch, is written
        # BEFORE the ledger move, so no crash can lose the watch. A record whose move is
        # missing leaves the money in flight (watch.decision_applied == "missing").
        st.record_resolution(
            kstore,
            kind,
            ref,
            {
                "resolution": item["resolution"],
                "evidence_ref": item["evidence_ref"],
                "request_id": item["request_id"],
                "amount": item["amount"],
                "first_sent_at": item["first_sent_at"],
                "owner_proposal_id": ctx.proposal_id,  # rule M4
                "decided_at": ctx.policy.now,
                "kind": kind,
                "ref": ref,
                # not_landed stays on the watch list: reconcile reads it back for good.
                "watch": "open" if not landed else "n/a",
            },
        )
        _apply(kstore, tenant, st.resolution(kstore, kind, ref))
        return ()

    def postcondition(ctx: StepContext) -> PostconditionResult:
        kstore = st.kit_store(ctx.policy)
        kind, ref = ctx.intent["kind"], ctx.intent["ref"]
        rec = st.resolution(kstore, kind, ref)
        if rec is None:
            return PostconditionResult(False, f"{kind} {ref} was not resolved")
        state = st.ledger_state(kstore, ctx.policy.tenant.tenant_id, st.profile(kstore).currency)
        if (problem := identity_problem(state, None)) is not None:
            return PostconditionResult(False, problem)
        return PostconditionResult(
            True, f"{kind} {ref} resolved as {rec['resolution']} on {rec['evidence_ref']}"
        )

    return KitWorkflowSpec(
        approver=ApproverScope.KIT_OPERATOR,  # the kit 8 operator approves
        kit=KIT,
        action="resolve_in_flight_money",
        validate=validate_resolve,
        policy=policy_fn,
        steps=(StepSpec("record_decision", build),),
        approval_ttl=APPROVAL_TTL,
        read_allowlist=TENANT_READS,
        platform_read_allowlist=PLATFORM_READS,
        postcondition=postcondition,
    )
