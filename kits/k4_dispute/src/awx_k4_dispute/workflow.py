"""SPECS: the four kit 4 actions as KitWorkflowSpec.

Every step re-reads the dispute at execution time, because approval can take hours: a
deadline that passed (checked against the build's own ctx.policy.now, rule 6), an amount that
changed, a policy version that moved or a dispute that was answered elsewhere stops the write.
A step that finds its own action already applied returns no operations, so a duplicate
delivery is a quiet no-op. Builds are idempotent (rule 0) and rebuild the identical operation
each time. The dispute lock (cases.py, rules M1 and M4) is owned by ctx.proposal_id: the first
build takes it (the upload step for a challenge, the accept step for an accept); the response
attempt is recorded only in the build that is about to send the accept or challenge, after
every upload succeeded. Only this run's postcondition releases the lock, and only on a
definitive outcome. Accept and challenge steps carry finality.dispute_reconcile as
StepSpec.reconcile (rule 9); the dispute endpoints do not deduplicate, so after an unknown
outcome nothing is resent and a person decides. There is no on_abandon: it runs only when no
step ran, so there is never a lock of its own to release.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from functools import partial
from typing import Any

from awx_common import Operation, OperationStatus
from awx_common.approvals import RiskTier
from awx_common.evidence import EvidenceRef
from awx_common.workflow import KitWorkflowSpec, PostconditionResult, StepContext, StepSpec

from .cases import (
    AttemptState,
    attempt_of,
    attempt_state,
    attempts,
    case_value,
    clear_attempt,
    ensure_book,
    finish,
    intent_digest,
    next_attempt_number,
    record_attempt,
    response_for,
    take_lock,
    update_case,
)
from .clearing import DEFERRED, clear_blocks, owner_finished
from .config import DEFAULT_CONFIG, KIT, DisputeConfig
from .deadlines import ActionKind, require_open
from .evidence import DOCUMENT_TYPE
from .finality import (
    ACCEPT_FINALITY,
    ANSWERED_BY_ACCEPT,
    ANSWERED_BY_CHALLENGE,
    CHALLENGE_FINALITY,
    dispute_reconcile,
)
from .intents import (
    evidence_from_computed,
    validate_accept,
    validate_challenge,
    validate_clear,
    validate_close,
    validate_refer,
)
from .policy import (
    accept_policy,
    amount_matches,
    challenge_policy,
    clear_policy,
    close_policy,
    read_view,
    refer_policy,
)
from .provider import (
    READ_ALLOWLIST,
    AcceptReason,
    ChallengeReason,
    DisputeView,
    accept_operation,
    challenge_operation,
    file_id_of,
    upload_operation,
)


class StaleDispute(Exception):
    """The dispute changed between approval and execution. Nothing is sent."""


class PolicyChanged(Exception):
    """The intent was approved under another policy version. Nothing is sent."""


class ChallengeAlreadySubmitted(Exception):
    """The provider shows an earlier challenge. This kit never sends a second one."""


class EvidenceUploadIncomplete(Exception):
    """A challenge cannot be built because an evidence upload has no file id."""


def _recheck(ctx: StepContext, action: ActionKind, config: DisputeConfig) -> DisputeView:
    intent = ctx.intent
    if intent.get("policy_version") != config.policy_version:
        raise PolicyChanged("intent policy version differs from the running policy")
    view = read_view(intent["dispute_id"], ctx.policy)
    if not amount_matches(intent, view):
        raise StaleDispute(f"disputed amount is now {view.amount}")
    approved = ctx.decision.computed.get("stage")
    if view.stage != approved:
        # The approver approved a response at one stage; the request_id
        # and body would be derived from another. Fail closed before any lock or attempt.
        raise StaleDispute(f"dispute stage is now {view.stage}; approved at {approved}")
    return view


def _ready(view: DisputeView, action: ActionKind, ctx: StepContext, config: DisputeConfig):
    if not view.is_respondable:
        raise StaleDispute(f"dispute status is {view.raw_status}; it no longer takes a response")
    if action is ActionKind.CHALLENGE and view.prior_challenge_seen:
        raise ChallengeAlreadySubmitted(view.dispute_id)
    require_open(view, action, config, ctx.policy.now)
    name = "challenge" if action is ActionKind.CHALLENGE else "accept"
    own = attempt_of(case_value(ctx.policy.store, view.dispute_id), name, ctx.proposal_id)
    if own is not None and own.get("cleared"):
        # A person cleared this run's response, so a redriven or
        # redelivered owner execution must never send it under the old key.
        raise StaleDispute(f"this run's {name} was cleared by a person; it is never sent")


def _lock(ctx: StepContext, action: str, view: DisputeView, config: DisputeConfig) -> None:
    """Take or keep the dispute lock for this run (M4). Idempotent for the owner."""
    take_lock(
        ctx.policy.store,
        view.dispute_id,
        ctx.proposal_id,
        action,
        view,
        ctx.policy.now,
        config.upload_lock_ttl,
    )


def _numbered(ctx: StepContext, action: str, view: DisputeView) -> None:
    """Re-derive the attempt number under the lock. If another run
    recorded an attempt at this stage since the proposal, the approved request_id is no longer
    this run's to use: fail closed with nothing sent."""
    case = case_value(ctx.policy.store, view.dispute_id)
    if attempt_of(case, action, ctx.proposal_id) is not None:
        return  # a redelivered build: this run's own number stands
    expected = next_attempt_number(case, action, view.stage)
    approved = ctx.decision.computed.get("response_attempt", 1)
    if expected != approved:
        raise StaleDispute(
            f"another {action} was recorded at {view.stage} since approval "
            f"(attempt {expected}, approved {approved}); propose again"
        )


def _mark(ctx: StepContext, action: str, request_id: str) -> None:
    """Record the response attempt just before the accept or challenge is sent. Idempotent."""
    record_attempt(
        ctx.policy.store,
        ctx.intent["dispute_id"],
        ctx.proposal_id,
        action,
        ctx.intent,
        ctx.decision,
        request_id,
        ctx.policy.now,
    )


def build_accept(ctx: StepContext, config: DisputeConfig = DEFAULT_CONFIG) -> Sequence[Operation]:
    view = _recheck(ctx, ActionKind.ACCEPT, config)
    if view.status in ANSWERED_BY_ACCEPT:
        return ()
    _ready(view, ActionKind.ACCEPT, ctx, config)
    op = accept_operation(
        view.dispute_id,
        view.stage,
        AcceptReason(ctx.intent["reason"]),
        ctx.intent_hash,
        ctx.decision.computed.get("response_attempt", 1),
    )
    _lock(ctx, "accept", view, config)
    _numbered(ctx, "accept", view)
    _mark(ctx, "accept", op.request_id)
    return (op,)


def _upload_ops(ctx: StepContext, dispute_id: str) -> list[tuple[str, Operation]]:
    out = []
    for file, sid in evidence_from_computed(ctx.decision.computed):
        ref = EvidenceRef(
            evidence_id=sid,
            sha256=file.sha256,
            file_name=file.file_name,
            content_type=file.content_type,
            size_bytes=file.size_bytes,
        )
        out.append(
            (
                DOCUMENT_TYPE[file.kind],
                upload_operation(
                    dispute_id,
                    file.evidence_id,
                    ref,
                    ctx.intent_hash,
                    book_id=ensure_book(ctx.policy.store),
                    run_id=ctx.proposal_id,
                ),
            )
        )
    return out


def build_uploads(ctx: StepContext, config: DisputeConfig = DEFAULT_CONFIG) -> Sequence[Operation]:
    view = _recheck(ctx, ActionKind.CHALLENGE, config)
    if view.status in ANSWERED_BY_CHALLENGE:
        return ()
    _ready(view, ActionKind.CHALLENGE, ctx, config)
    _lock(ctx, "challenge", view, config)  # the lock only; the attempt waits for the uploads
    return tuple(op for _, op in _upload_ops(ctx, view.dispute_id))


def build_challenge(
    ctx: StepContext, config: DisputeConfig = DEFAULT_CONFIG
) -> Sequence[Operation]:
    view = _recheck(ctx, ActionKind.CHALLENGE, config)
    if view.status in ANSWERED_BY_CHALLENGE:
        return ()
    _ready(view, ActionKind.CHALLENGE, ctx, config)
    uploads = {r.request_id: r for r in ctx.prior.get("upload_evidence", ())}
    documents: list[tuple[str, str]] = []
    for doc_type, op in _upload_ops(ctx, view.dispute_id):
        result = uploads.get(op.request_id)
        file_id = (
            file_id_of(result.body, result.provider_id)
            if result is not None and result.status is OperationStatus.SUCCEEDED
            else None
        )
        if file_id is None:
            raise EvidenceUploadIncomplete(f"{op.body['notes']} has no uploaded file id")
        documents.append((doc_type, file_id))
    op = challenge_operation(
        view.dispute_id,
        view.stage,
        ChallengeReason(ctx.intent["reason"]),
        documents,
        ctx.decision.computed.get("product_type", config.product_type),
        config.product_description,
        ctx.intent_hash,
        ctx.decision.computed.get("response_attempt", 1),
    )
    _lock(ctx, "challenge", view, config)
    _numbered(ctx, "challenge", view)
    _mark(ctx, "challenge", op.request_id)
    return (op,)


def _no_ops(ctx: StepContext) -> Sequence[Operation]:
    return ()


def _results(ctx: StepContext) -> list[dict[str, Any]]:
    return [
        {
            "step": step,
            "request_id": r.request_id,
            "status": r.status.value,
            "provider_status": r.provider_status,
            "error_type": r.error_type,
        }
        for step, results in sorted(ctx.prior.items())
        for r in results
    ]


RESPONSE_STEPS = ("accept", "submit_challenge")


def _landed_by(case, what: str, stage, view, exclude: str | None = None) -> str:
    at_stage = [
        a
        for a in attempts(case, what)
        if a["decision"]["computed"].get("stage") == stage and a.get("owner") != exclude
    ]
    live = [
        a["owner"]
        for a in at_stage
        if not a.get("cleared") and attempt_state(case, a, view) is AttemptState.LANDED
    ]
    if live:
        return "proposal " + ", ".join(sorted(live))
    cleared = [a["owner"] for a in at_stage if a.get("cleared")]
    if cleared:
        return "cleared proposal " + ", ".join(sorted(cleared)) + " (landed late)"
    return "a response made outside this kit"


def _status_post(answered: frozenset, what: str):
    """Runs after success AND after a failed or unknown operation (rule 2). Records this run's
    response, then releases the lock ONLY on a definitive outcome (M1, M4):

    - LANDED: the provider records this response at its stage, or the dispute reads back in a
      responded state at that stage
    - REFUSED: the last result of every accept or challenge this run sent is FAILED. The
      dispute endpoints are never resent (they do not deduplicate), so this is always a first
      send's refusal, which is definitive (M1). A refused RESEND (an uncertain send) comes back
      OUTCOME_UNKNOWN, with the refusal in body["refused"], and is in flight
    - nothing to judge: this run recorded no response attempt (an upload failed, or a build
      refused before recording), so nothing was handed to the executor; the lock is released.
      An attempt this run DID record with no result in ctx.prior is IN_FLIGHT, never "not
      sent": the attempt is recorded just before the operation goes to the executor, and the
      runner can drop a step's result after an error

    Otherwise (OUTCOME_UNKNOWN including the uncertain-send `refused` and `multiple_objects`
    shapes, a finality timeout, or a SUCCEEDED send the dispute does not yet show) the response
    is IN_FLIGHT, the lock stays held, and every new response proposal is BLOCKED for a person
    until the dispute shows the outcome."""

    def post(ctx: StepContext) -> PostconditionResult:
        view = read_view(ctx.intent["dispute_id"], ctx.policy)
        store = ctx.policy.store
        last: dict[str, Any] = {}
        for step in RESPONSE_STEPS:
            for r in ctx.prior.get(step, ()):
                last[r.request_id] = r
        sent = list(last.values())
        case = case_value(store, view.dispute_id)
        attempt = attempt_of(case, what, ctx.proposal_id)
        mine = attempt is not None and attempt.get("owner") == ctx.proposal_id
        stage = ctx.decision.computed.get("stage")
        answered_elsewhere = False
        landed = view.responded(what, stage) or (
            view.status in answered and (stage is None or view.stage == stage)
        )
        own_refused = bool(sent) and all(r.status is OperationStatus.FAILED for r in sent)
        others_here = [
            x
            for x in attempts(case, what)
            if x.get("owner") != ctx.proposal_id and x["decision"]["computed"].get("stage") == stage
        ]
        if landed and mine and own_refused and others_here:
            # This run's own send was refused while the stage shows a
            # response and another run of this kit also responded at this stage: it was
            # answered by that run. Record REFUSED, never this run's LANDED. With no other kit
            # attempt, a refusal of a landed response is most likely this run's own duplicate
            # delivery, so it stays LANDED (the provider record carries no request_id to tell).
            state = AttemptState.REFUSED
            landed = False
            answered_elsewhere = True
        elif landed:
            state = AttemptState.LANDED
        elif own_refused:
            state = AttemptState.REFUSED
        elif not sent and not mine:
            finish(store, view.dispute_id, ctx.proposal_id, None, release=True)
            return PostconditionResult(False, f"{what}: nothing sent; the case stays open")
        else:
            state = AttemptState.IN_FLIGHT
        response = None
        if mine and (sent or response_for(case, attempt) is None):  # a redelivery keeps it
            response = {
                "action": what,
                "owner": ctx.proposal_id,
                "intent_digest": intent_digest(ctx.intent),
                "state": state.value,
                "reason": ctx.intent["reason"],
                "evidence_sha256": list(ctx.decision.computed.get("evidence_sha256", [])),
                "economics": {
                    k: ctx.decision.computed.get(k)
                    for k in ("cost_accept", "cost_contest_expected", "recommendation")
                },
                "results": _results(ctx),
                "provider_status": view.raw_status,
                "respond_by": ctx.decision.computed.get("respond_by"),
            }
        finish(
            store,
            view.dispute_id,
            ctx.proposal_id,
            response,
            release=state is not AttemptState.IN_FLIGHT,
        )
        if landed and not mine:
            # This run sent nothing.
            # Name every other attempt at this stage that reads LANDED and is not cleared; a
            # cleared one is named only when it is all there is (it landed late).
            return PostconditionResult(
                False,
                f"{what}: already done by {_landed_by(case, what, stage, view)}; "
                "this run sent nothing",
            )
        if answered_elsewhere:
            return PostconditionResult(
                False,
                f"{what}: REFUSED, answered by another run "
                f"({_landed_by(case, what, stage, view, exclude=ctx.proposal_id)})",
            )
        detail = f"{what}: {state.value}, provider status {view.raw_status}"
        if state is AttemptState.IN_FLIGHT:
            due = ctx.decision.computed.get("respond_by")
            rid = attempt.get("request_id") if attempt else None
            detail += f"; a person must reconcile request_id {rid} before {due}; nothing is resent"
        return PostconditionResult(landed, detail)

    return post


def _close_post(ctx: StepContext) -> PostconditionResult:
    view = read_view(ctx.intent["dispute_id"], ctx.policy)
    entries: Any = ctx.decision.computed.get("ledger_entries")
    if not (view.is_terminal and entries):
        return PostconditionResult(False, f"cannot close at {view.raw_status}")

    def change(case: dict[str, Any]) -> dict[str, Any]:
        if not case.get("outcome"):  # first close wins; a duplicate delivery changes nothing
            case["outcome"] = {
                "close_reason": ctx.intent["close_reason"],
                "provider_status": view.raw_status,
                "ledger_entries": entries,
                "proposal_hash": ctx.intent_hash,
            }
        return case

    update_case(ctx.policy.store, view.dispute_id, change)
    return PostconditionResult(True, f"closed as {ctx.intent['close_reason']} at {view.raw_status}")


def _referral_post(ctx: StepContext) -> PostconditionResult:
    def change(case: dict[str, Any]) -> dict[str, Any]:
        refs = case.get("referrals", [])
        if any(r.get("proposal_hash") == ctx.intent_hash for r in refs):
            return case
        case["referrals"] = [
            *refs,
            {
                "summary": ctx.intent["summary"],
                "question": ctx.intent["question"],
                "proposal_hash": ctx.intent_hash,
            },
        ]
        return case

    update_case(ctx.policy.store, ctx.intent["dispute_id"], change)
    return PostconditionResult(True, f"referred {ctx.intent['dispute_id']} to a person")


def _clear_post(ctx: StepContext) -> PostconditionResult:
    """Re-read fresh (rule 6) and clear only if the read-back is still empty, so a response
    that landed between approval and now is recorded LANDED instead. Idempotent."""
    computed = ctx.decision.computed
    case = case_value(ctx.policy.store, ctx.intent["dispute_id"])
    target = next(
        (a for a in attempts(case) if a.get("request_id") == computed["attempt_request_id"]),
        None,
    )
    if target is None:
        return PostconditionResult(False, "the attempt is gone; nothing cleared")
    # The same rule again, now, so an early approval cannot apply
    # early. No deferral here: the postcondition's context always has run_status.
    waits, _ = clear_blocks(ctx.policy, target)
    owner = owner_finished(ctx.policy, target)
    if owner == DEFERRED:
        waits.append("the owner run's status cannot be read here")
    if waits:
        return PostconditionResult(False, "not cleared: " + "; ".join(waits))
    view = read_view(ctx.intent["dispute_id"], ctx.policy)
    stage, action = computed["attempt_stage"], computed["attempt_action"]
    if view.responded(action, stage) or (
        view.status in ANSWERED_BY.get(action, frozenset()) and view.stage == stage
    ):
        return PostconditionResult(False, f"the {action} landed after all; nothing cleared")
    if not (view.is_respondable and view.stage == stage):
        return PostconditionResult(False, f"read-back is not empty ({view.raw_status}); kept")
    clear_attempt(
        ctx.policy.store,
        view.dispute_id,
        computed["attempt_owner"],
        computed["attempt_request_id"],
        cleared_by=ctx.proposal_id,
        at=ctx.policy.now,
        owner_terminal=True,
    )
    return PostconditionResult(True, f"cleared {computed['attempt_request_id']}; still watched")


ANSWERED_BY = {"accept": ANSWERED_BY_ACCEPT, "challenge": ANSWERED_BY_CHALLENGE}


def build_specs(config: DisputeConfig = DEFAULT_CONFIG) -> dict[str, KitWorkflowSpec]:
    common = {"kit": KIT, "approval_ttl": config.approval_ttl, "read_allowlist": READ_ALLOWLIST}
    specs = (
        KitWorkflowSpec(
            action="accept_dispute",
            validate=partial(validate_accept, config=config),
            policy=partial(accept_policy, config=config),
            steps=(
                StepSpec(
                    "accept",
                    partial(build_accept, config=config),
                    ACCEPT_FINALITY,
                    reconcile=dispute_reconcile,
                ),
            ),
            postcondition=_status_post(ANSWERED_BY_ACCEPT, "accept"),
            **common,
        ),
        KitWorkflowSpec(
            action="challenge_dispute",
            validate=partial(validate_challenge, config=config),
            policy=partial(challenge_policy, config=config),
            steps=(
                StepSpec("upload_evidence", partial(build_uploads, config=config)),
                StepSpec(
                    "submit_challenge",
                    partial(build_challenge, config=config),
                    CHALLENGE_FINALITY,
                    reconcile=dispute_reconcile,
                ),
            ),
            postcondition=_status_post(ANSWERED_BY_CHALLENGE, "challenge"),
            **common,
        ),
        KitWorkflowSpec(
            action="refer_dispute",
            validate=partial(validate_refer, config=config),
            policy=partial(refer_policy, config=config),
            steps=(StepSpec("record_referral", _no_ops),),
            postcondition=_referral_post,
            # A referral moves no money and writes only a note for a person.
            agent_min_tier=RiskTier.AUTO,
            **common,
        ),
        KitWorkflowSpec(
            action="close_dispute",
            validate=partial(validate_close, config=config),
            policy=partial(close_policy, config=config),
            steps=(StepSpec("record_outcome", _no_ops),),
            postcondition=_close_post,
            # Close records an outcome Airwallex already reached; it sends nothing.
            agent_min_tier=RiskTier.AUTO,
            **common,
        ),
        KitWorkflowSpec(
            action="clear_dispute_response",
            validate=partial(validate_clear, config=config),
            policy=partial(clear_policy, config=config),
            steps=(StepSpec("record_clearance", _no_ops),),
            postcondition=_clear_post,
            **common,
        ),
    )
    return {s.action: s for s in specs}


SPECS: dict[str, KitWorkflowSpec] = build_specs()


def steps_by_name(spec: KitWorkflowSpec) -> Mapping[str, StepSpec]:
    return {s.name: s for s in spec.steps}
