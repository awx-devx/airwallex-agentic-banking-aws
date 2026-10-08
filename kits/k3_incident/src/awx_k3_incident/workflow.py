"""Kit 3 workflow specs. SPECS is keyed by action; the propose tool is propose_<action>.

replace_failed_transfer    the incident action: at most one replacement for one failed transfer
close_incident             records the closure once both dispositions are terminal
send_supplier_transfer     OPERATOR STAGING ONLY (STAGING_SPECS, no agent tool, not deployed):
                           the demo's original payment, capped and limited to poc-i3- payees
simulate_transfer_failure  sandbox only: SENT, then FAILED with a failure_type, ends CANCELLED

Every build step re-runs its checks against fresh reads, because an approval can sit for hours
while the original changes state. Rule 1 of the runner re-runs the policy too, so a changed
original turns the approval STALE before any step runs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import timedelta
from typing import Any

from awx_common import Money, Operation, OperationResult, OperationStatus, RiskTier
from awx_common.errors import StateConflict
from awx_common.sandbox import make_transfer_advance
from awx_common.workflow import (
    FinalitySpec,
    KitWorkflowSpec,
    PolicyContext,
    PolicyDecision,
    PostconditionResult,
    SandboxAdvance,
    StepContext,
    StepSpec,
)

from . import airwallex as awx
from . import ledger
from .closure import IncidentNotClosable, closure_record, record_closure
from .finality import IncidentDecision, failure_code_of
from .intent import validate_close, validate_replace, validate_send, validate_simulate
from .policy import (
    evaluate_incident,
    funding_problem,
    listed_past_margin,
    replace_policy,
    send_policy,
    simulate_policy,
)


class ReplacementRefused(StateConflict):
    """The build step's re-check failed. No replacement is sent."""


def _object_id(result: OperationResult) -> str:
    oid = result.provider_id or (result.body or {}).get("id")
    if not isinstance(oid, str) or not oid:
        raise StateConflict("operation result names no transfer id to poll")
    return oid


def transfer_finality(
    terminal: set[str],
    success: set[str],
    *,
    sandbox_advance: SandboxAdvance | None = None,
    **kw: int,
) -> FinalitySpec:
    return FinalitySpec(
        read_request=lambda r: awx.transfer_read(_object_id(r)),
        status_of=lambda body: str(body.get("status") or ""),
        terminal=frozenset(terminal),
        success=frozenset(success),
        sandbox_advance=sandbox_advance,
        **kw,
    )


# ---- replace_failed_transfer -------------------------------------------------------------

_PLAN_KEYS = (
    "attempt",
    "replacement_request_id",
    "beneficiary_id",
    "amount",
    "transfer_method",
    "fee",
    "required_funds",
    "reference",
)
# A FAILED transfers/create that is a definitive refusal of a first send (M1): Airwallex
# rejected the body and created nothing. Everything else (OUTCOME_UNKNOWN, a refused resend
# after an uncertain send, two objects under one request_id, a read-back mismatch, a
# reconciled CANCELLED object) keeps the money in flight or goes to a person.
_REFUSALS = frozenset({"InsufficientFunds", "ValidationFailed", "LimitExceeded"})
# OUTCOME_UNKNOWN codes rule 9 stops on at once: a person decides.
_PERSON_CODES = frozenset(
    {
        "request_id_bound_to_another_body",
        "request_id_reused",
        "multiple_objects",
        "first_attempt_unknown",
        "past_dedupe_margin",
    }
)
_NOT_DEFINITIVE_CODES = frozenset(
    {"multiple_objects", "request_id_reused", "provider_failure_state"}
)


def replacement_op(computed: Mapping[str, Any], intent_hash: str) -> Operation:
    """The replacement transfer, a pure function of the original and the attempt (never of the
    incident), so every path that reaches it builds the identical body under the identical
    request_id."""
    oid = computed["original_transfer_id"]
    attempt = computed["attempt"]
    return awx.create_transfer_op(
        business_key=f"transfer:{oid}:{attempt}",
        request_id=computed["replacement_request_id"],
        intent_hash=intent_hash,
        beneficiary_id=computed["beneficiary_id"],
        amount=computed["amount"],
        transfer_method=computed["transfer_method"],
        reference=computed["reference"],
        metadata={"poc_label": f"{awx.POC_LABEL}replacement", "replaces_transfer_id": oid},
    )


def build_replacement(ctx: StepContext) -> Sequence[Operation]:
    """Re-check against fresh reads, take or join the lock, then emit the one Operation.

    - our own replacement already listed (a redelivered build): re-emit it; the runner settles
      it as a duplicate.
    - the lock is ours for this attempt (redelivery): re-emit.
    - a live lock from another run (M1 resume): JOIN it first with a versioned write (the
      owner keeps ownership, M4, but can no longer release it), then re-emit the identical
      Operation; rule 9 reads back before any resend. A lost join race sends nothing.
    - otherwise: take the lock for this attempt, then emit. A lost race sends nothing.
    """
    approved = ctx.decision.computed
    oid = ctx.intent["original_transfer_id"]
    if approved.get("original_transfer_id") != oid:
        raise ReplacementRefused("approved plan is for a different original transfer")
    attempt = approved.get("attempt")
    if not isinstance(attempt, int) or approved.get(
        "replacement_request_id"
    ) != awx.replacement_request_id(oid, attempt):
        raise ReplacementRefused("approved request_id is not the original's replacement id")
    store, now = ctx.policy.store, ctx.policy.now
    rid = approved["replacement_request_id"]
    op = replacement_op(approved, ctx.intent_hash)
    lock = ledger.get_lock(store, oid)
    if lock is not None and lock.attempt == attempt and lock.state in ledger.LIVE_STATES:
        if lock.owner != ctx.proposal_id:
            if lock.past_margin(now):
                raise ReplacementRefused(
                    "in flight past the deduplication margin: a person decides"
                )
            assert store is not None
            try:
                ledger.join_lock(store, lock, ctx.proposal_id, now)
            except ledger.LedgerConflict as exc:
                raise ReplacementRefused(str(exc)) from exc
        return (op,)
    listed = awx.find_transfers_by_request_id(ctx.policy.read, rid)
    if listed:
        if any(listed_past_margin(t, now) for t in listed):
            raise ReplacementRefused("listed replacement past the deduplication margin: a person")
        return (op,)
    ev = evaluate_incident(ctx.policy.read, oid, store, now)
    if ev.decision is not IncidentDecision.REPLACE or ev.plan is None:
        raise ReplacementRefused(f"re-check: {ev.decision}: {'; '.join(ev.reasons)}")
    fresh = ev.plan.computed()
    changed = [k for k in _PLAN_KEYS if fresh[k] != approved.get(k)]
    if changed:
        raise ReplacementRefused(f"original changed since approval: {changed}")
    if store is None:
        raise ReplacementRefused("no kit store: the replacement lock cannot be taken")
    try:
        ledger.take_lock(
            store,
            original_transfer_id=oid,
            owner=ctx.proposal_id,
            attempt=attempt,
            request_id=rid,
            now=now,
            over=lock,
        )
    except ledger.LedgerConflict as exc:
        raise ReplacementRefused(str(exc)) from exc
    return (op,)


def _definitive_refusal(result: OperationResult) -> bool:
    body = result.body or {}
    return (
        result.status is OperationStatus.FAILED
        and result.error_type in _REFUSALS
        and result.error_code not in _NOT_DEFINITIVE_CODES
        # A resend after an uncertain send is never definitive. The spine reports it as
        # OUTCOME_UNKNOWN with body["last_error"] (and "refused"); the status check above
        # covers it, the markers are kept as a defence if a result is ever rewritten.
        and "refused" not in body
        and "last_error" not in body
        and "_awx_recovered_from" not in body
    )


def _settle_lock(ctx: StepContext, oid: str) -> str:
    """Move the lock from what this run has READ (M1, M4). Returns a short note."""
    store, now = ctx.policy.store, ctx.policy.now
    approved = ctx.decision.computed
    attempt = approved.get("attempt")
    lock = ledger.get_lock(store, oid)
    if store is None or lock is None or lock.attempt != attempt:
        return "no lock of this attempt"
    found = awx.find_transfers_by_request_id(ctx.policy.read, lock.request_id)
    try:
        if found:
            status = awx.get_transfer(ctx.policy.read, str(found[0].get("id"))).get("status")
            if status == "CANCELLED":
                if lock.state != ledger.REPLACEMENT_FAILED:
                    ledger.move_lock(
                        store, lock, ledger.REPLACEMENT_FAILED, now, "replacement CANCELLED"
                    )
                return "replacement CANCELLED; a person decides"
            if lock.state != ledger.LANDED:
                ledger.move_lock(store, lock, ledger.LANDED, now, f"listed as {status}")
            return f"replacement landed ({status})"
        if lock.owner != ctx.proposal_id and lock.state in ledger.LIVE_STATES and lock.joined_by:
            # A joiner (this run, or another) may have sent, and nobody may release a joined
            # lock: record it as in flight and tell a person.
            if lock.state != ledger.IN_FLIGHT:
                ledger.move_lock(store, lock, ledger.IN_FLIGHT, now, "joined; seen by a joiner")
            return (
                f"NEEDS_PERSON: joined by {len(lock.joined_by)} run(s); in flight on "
                f"{lock.request_id} until it lands, a person decides, or the dedupe margin passes"
            )
        if lock.owner != ctx.proposal_id or lock.state not in ledger.LIVE_STATES:
            return f"replacement {lock.state}; not ours to release"
        if lock.joined_by:
            # Another run may have sent on this attempt: this run's own evidence (nothing
            # sent, or refused) does not cover that send, so the money stays in flight.
            if lock.state != ledger.IN_FLIGHT:
                ledger.move_lock(store, lock, ledger.IN_FLIGHT, now, "joined by another run")
            return (
                f"NEEDS_PERSON: joined by {len(lock.joined_by)} other run(s), so this run cannot "
                f"release it; in flight on {lock.request_id} until it lands, a person decides, "
                "or the dedupe margin passes"
            )
        if REPLACE_STEP.name not in ctx.prior:
            # The runner records the step's key before it dispatches anything, so a missing
            # key means the build (or the runner's check of what it built) failed: nothing left.
            ledger.move_lock(store, lock, ledger.RELEASED, now, "nothing was sent")
            return "nothing was sent; lock released"
        results = ctx.prior[REPLACE_STEP.name]
        if not results:
            # prior holds every dispatched operation, so an empty list means the step's
            # one operation was refused at its first send before anything left (for example
            # NotAuthorised or TenantScopeViolation). Kept IN_FLIGHT anyway: the next run
            # resumes the same Operation, which sends it only if it never left.
            if lock.state != ledger.IN_FLIGHT:
                ledger.move_lock(store, lock, ledger.IN_FLIGHT, now, "dispatched, no result")
            return (
                f"NEEDS_PERSON: the step recorded no result (refused before its first send); "
                f"in flight on {lock.request_id}; a person can check the operation record"
            )
        last = results[-1]
        if last.error_code in _PERSON_CODES:
            # Rule 9 stops at once on these; no resend can change them.
            if lock.state != ledger.IN_FLIGHT:
                ledger.move_lock(store, lock, ledger.IN_FLIGHT, now, last.error_code)
            return (
                f"NEEDS_PERSON: {last.error_code} on {lock.request_id}; in flight, nothing more "
                "is sent automatically"
            )
        if _definitive_refusal(last):
            ledger.move_lock(
                store, lock, ledger.RELEASED, now, f"refused at first send: {last.error_type}"
            )
            return f"refused at first send ({last.error_type}); lock released"
        if lock.state != ledger.IN_FLIGHT:
            ledger.move_lock(
                store, lock, ledger.IN_FLIGHT, now, f"{last.status.value} {last.error_code or ''}"
            )
        return f"in flight on {lock.request_id} ({last.status.value}); lock kept"
    except ledger.LedgerConflict as exc:
        return f"NEEDS_PERSON: lock not moved ({exc}); in flight, a person checks the lock"


def _try_close(ctx_policy: PolicyContext, oid: str) -> PostconditionResult:
    """Close the incident if both dispositions are terminal; record it once in the store."""
    found = awx.find_replacements(ctx_policy.read, oid)
    if len(found) != 1:
        return PostconditionResult(False, f"expected exactly one replacement, found {len(found)}")
    rid = str(found[0].get("request_id"))
    original = awx.get_transfer(ctx_policy.read, oid)
    replacement = awx.get_transfer(ctx_policy.read, str(found[0].get("id")))
    try:
        closure = closure_record({**original, "id": oid}, replacement, replacement_request_id=rid)
        record_closure(ctx_policy.store, closure)
    except IncidentNotClosable as exc:
        return PostconditionResult(False, f"incident not closable yet: {exc}")
    if not closure.resolved:
        return PostconditionResult(
            False,
            f"replacement {closure.replacement_transfer_id} also ended CANCELLED; escalate",
        )
    return PostconditionResult(
        True,
        f"closed: original {oid} CANCELLED ({closure.original_failure_code}); "
        f"replacement {closure.replacement_transfer_id} PAID",
    )


def replacement_postcondition(ctx: StepContext) -> PostconditionResult:
    oid = ctx.intent["original_transfer_id"]
    note = _settle_lock(ctx, oid)
    closed = _try_close(ctx.policy, oid)
    return PostconditionResult(closed.ok, f"{closed.detail}; {note}")


# The replacement settles in the sandbox through rule 10's driver: its reference never carries
# the no-settle marker, so awx_common.sandbox's driver takes it to SENT, then PAID.
# Without the driver the poll times out after 5 minutes, the postcondition keeps the lock
# IN_FLIGHT and reports "not closable yet", and close_incident records the closure once the
# replacement is terminal.
REPLACE_STEP = StepSpec(
    "replace",
    build_replacement,
    transfer_finality({"PAID", "CANCELLED"}, {"PAID"}, interval_s=10, timeout_s=300),
    reconcile=awx.reconcile_transfer_create,
)

REPLACE_SPEC = KitWorkflowSpec(
    kit=awx.KIT,
    action="replace_failed_transfer",
    validate=validate_replace,
    policy=replace_policy,
    steps=(REPLACE_STEP,),
    approval_ttl=timedelta(hours=4),
    read_allowlist=awx.READ_ALLOWLIST,
    postcondition=replacement_postcondition,
)


# ---- close_incident ----------------------------------------------------------------------


def close_policy(intent: Mapping[str, Any], ctx: PolicyContext) -> PolicyDecision:
    oid = intent["original_transfer_id"]
    found = awx.find_replacements(ctx.read, oid)
    if len(found) != 1:
        return PolicyDecision(RiskTier.BLOCKED, (f"{len(found)} replacements found; need one",))
    rid = str(found[0].get("request_id"))
    original = awx.get_transfer(ctx.read, oid)
    replacement = awx.get_transfer(ctx.read, str(found[0].get("id")))
    try:
        closure = closure_record({**original, "id": oid}, replacement, replacement_request_id=rid)
    except IncidentNotClosable as exc:
        return PolicyDecision(RiskTier.BLOCKED, (f"not closable: {exc}",))
    return PolicyDecision(
        RiskTier.AUTO,
        ("records a closure; no money moves and nothing is written to Airwallex",),
        closure.record(),
    )


def build_close(ctx: StepContext) -> Sequence[Operation]:
    return ()


def close_postcondition(ctx: StepContext) -> PostconditionResult:
    oid = ctx.intent["original_transfer_id"]
    lock = ledger.get_lock(ctx.policy.store, oid)
    if lock is not None and ctx.policy.store is not None and lock.state != ledger.LANDED:
        found = awx.find_transfers_by_request_id(ctx.policy.read, lock.request_id)
        if found and found[0].get("status") == "PAID":
            try:  # a definitive outcome read: any run may record it (M4)
                ledger.move_lock(ctx.policy.store, lock, ledger.LANDED, ctx.policy.now, "PAID")
            except ledger.LedgerConflict:
                pass
    return _try_close(ctx.policy, oid)


CLOSE_SPEC = KitWorkflowSpec(
    kit=awx.KIT,
    action="close_incident",
    validate=validate_close,
    policy=close_policy,
    steps=(StepSpec("record", build_close),),
    approval_ttl=timedelta(hours=4),
    read_allowlist=awx.READ_ALLOWLIST,
    postcondition=close_postcondition,
    # Justified in README: no Airwallex write and no money movement, only a store record that
    # the policy derives entirely from two provider reads.
    agent_min_tier=RiskTier.AUTO,
)


# ---- send_supplier_transfer --------------------------------------------------------------


def build_send(ctx: StepContext) -> Sequence[Operation]:
    key = ctx.intent["payment_key"]
    approved = ctx.decision.computed
    if approved.get("payment_key") != key:
        raise StateConflict("approved plan is for another payment_key")
    store, now = ctx.policy.store, ctx.policy.now
    if store is None:
        raise StateConflict("no kit store: the book id cannot be minted")
    rid = awx.original_request_id(ledger.ensure_book(store, now), key)
    sent = ledger.sent_record(store, key)
    if sent is not None:
        if sent.get("request_id") != rid:
            raise StateConflict("payment_key is recorded under another request_id")
        if not awx.find_transfers_by_request_id(ctx.policy.read, rid) and (
            now - sent["sent_at"] >= ledger.DEDUPE_MARGIN
        ):
            raise StateConflict("in flight past the deduplication margin: a person decides")
    elif not awx.find_transfers_by_request_id(ctx.policy.read, rid):
        shortfall = funding_problem(ctx.policy.read, approved["required_funds"])
        if shortfall is not None:
            raise StateConflict(shortfall)
    try:
        ledger.claim_send(store, payment_key=key, request_id=rid, owner=ctx.proposal_id, now=now)
    except ledger.LedgerConflict as exc:
        raise StateConflict(str(exc)) from exc
    amount: Money = approved["amount"]
    return (
        awx.create_transfer_op(
            business_key=f"payment:{key}",
            request_id=rid,
            intent_hash=ctx.intent_hash,
            beneficiary_id=approved["beneficiary_id"],
            amount=amount,
            transfer_method=approved["transfer_method"],
            reference=approved["reference"],
            metadata={"poc_label": f"{awx.POC_LABEL}original", "payment_key": key},
        ),
    )


def send_postcondition(ctx: StepContext) -> PostconditionResult:
    book = ledger.book_id(ctx.policy.store)
    if book is None:
        return PostconditionResult(False, "no book: nothing was sent")
    rid = awx.original_request_id(book, ctx.intent["payment_key"])
    found = awx.find_transfers_by_request_id(ctx.policy.read, rid)
    if len(found) != 1:
        return PostconditionResult(
            False, f"expected one transfer for the payment, found {len(found)}; in flight"
        )
    return PostconditionResult(True, f"transfer {found[0].get('id')} created")


SEND_SPEC = KitWorkflowSpec(
    kit=awx.KIT,
    action="send_supplier_transfer",
    validate=validate_send,
    policy=send_policy,
    # No finality step: the demo deliberately leaves the original in flight.
    steps=(StepSpec("send", build_send, reconcile=awx.reconcile_transfer_create),),
    approval_ttl=timedelta(hours=4),
    read_allowlist=awx.STAGING_READ_ALLOWLIST,
    postcondition=send_postcondition,
)


# ---- simulate_transfer_failure (sandbox only) --------------------------------------------


def _current(ctx: StepContext) -> Mapping[str, Any]:
    transfer = awx.get_transfer(ctx.policy.read, ctx.intent["transfer_id"])
    if not awx.is_ours(transfer):
        raise StateConflict("transfer was not created by kit 3")
    return transfer


def build_to_sent(ctx: StepContext) -> Sequence[Operation]:
    status = _current(ctx).get("status")
    if status == "SENT":
        return ()
    if status not in awx.SIM_SENT_FROM:
        raise StateConflict(f"cannot move a {status} transfer to SENT")
    return (
        awx.transition_op(
            transfer_id=ctx.intent["transfer_id"], next_status="SENT", intent_hash=ctx.intent_hash
        ),
    )


def build_to_failed(ctx: StepContext) -> Sequence[Operation]:
    current = _current(ctx)
    status = current.get("status")
    if status == "CANCELLED" and failure_code_of(current)[0] is not None:
        return ()  # a retried build after the transition already landed
    if status != "SENT":  # the sandbox allows FAILED only from SENT
        raise StateConflict(f"cannot fail a {status} transfer; it must be SENT")
    return (
        awx.transition_op(
            transfer_id=ctx.intent["transfer_id"],
            next_status="FAILED",
            failure_type=ctx.intent["failure_type"],
            intent_hash=ctx.intent_hash,
        ),
    )


def simulate_postcondition(ctx: StepContext) -> PostconditionResult:
    transfer = awx.get_transfer(ctx.policy.read, ctx.intent["transfer_id"])
    code, _ = failure_code_of(transfer)
    if transfer.get("status") != "CANCELLED":
        return PostconditionResult(False, f"transfer is {transfer.get('status')}, not CANCELLED")
    if code is None:
        return PostconditionResult(False, "CANCELLED without a failure code")
    # The sandbox does not echo the simulated type (FAKE-FIDELITY FINDING), so the code is
    # reported, not compared with the requested type.
    return PostconditionResult(
        True, f"CANCELLED with failure code {code} (requested {ctx.intent['failure_type']})"
    )


SIMULATE_SPEC = KitWorkflowSpec(
    kit=awx.KIT,
    action="simulate_transfer_failure",
    validate=validate_simulate,
    policy=simulate_policy,
    steps=(
        StepSpec(
            "to_sent",
            build_to_sent,
            transfer_finality({"SENT", "PAID", "CANCELLED"}, {"SENT"}, interval_s=2, timeout_s=60),
            reconcile=awx.make_transition_reconcile(frozenset({"SENT", "PAID", "CANCELLED"})),
        ),
        StepSpec(
            "to_failed",
            build_to_failed,
            # FAILED is never readable: reads go straight to CANCELLED.
            transfer_finality({"CANCELLED", "PAID"}, {"CANCELLED"}, interval_s=2, timeout_s=60),
            reconcile=awx.make_transition_reconcile(frozenset({"CANCELLED"})),
        ),
    ),
    approval_ttl=timedelta(minutes=30),
    read_allowlist=awx.READ_ALLOWLIST,
    postcondition=simulate_postcondition,
    # agent_min_tier stays SINGLE: the model starts this, so a person approves it.
)


def build_specs(sandbox_advance: SandboxAdvance | None = None) -> dict[str, KitWorkflowSpec]:
    """SPECS, with `sandbox_advance` wired into the replacement's finality (rule 10).

    Only the replacement gets the driver: the original is created without finality, and the
    simulation steps make their own transitions.
    """
    replace_spec = REPLACE_SPEC
    if sandbox_advance is not None:
        finality = replace(REPLACE_STEP.finality, sandbox_advance=sandbox_advance)
        replace_spec = replace(REPLACE_SPEC, steps=(replace(REPLACE_STEP, finality=finality),))
    return {s.action: s for s in (replace_spec, CLOSE_SPEC, SIMULATE_SPEC)}


# The deployed specs carry the sandbox driver (runner rule 10). Only the replacement
# gets it; the staged original's reference carries "-nosettle" and has no finality step.
SPECS: dict[str, KitWorkflowSpec] = build_specs(make_transfer_advance(awx.KIT))

# The operator staging path: run only by an operator through the
# runner (Origin.OPERATOR), e.g. the live acceptance test. Not in SPECS, so KitStack builds no
# state machine for it and the Gateway registers no tool.
STAGING_SPECS: dict[str, KitWorkflowSpec] = {SEND_SPEC.action: SEND_SPEC}

__all__ = ["SPECS", "STAGING_SPECS", "ReplacementRefused", "build_specs"]
