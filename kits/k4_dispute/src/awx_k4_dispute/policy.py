"""Policy and the approval matrix. Code decides the tier; nothing here asks the model.

accept (irreversible, never AUTO)
    SINGLE   disputed amount below the dual-approval threshold
    DUAL     disputed amount at or above the threshold
    BLOCKED  not respondable, act-by passed, amount differs from the provider, a refund already
             exists, LOW_VALUE while contesting could still pay, AFTER_ISSUER_REJECTION at RFI,
             no economics for the currency, policy version moved
challenge (irreversible, never AUTO)
    SINGLE   economics say CONTEST and the amount is below the threshold
    DUAL     amount at or above the threshold, or economics say REFER
    BLOCKED  economics say ACCEPT, evidence band below the minimum, minimum evidence missing,
             reason does not fit the reason family, a prior challenge is visible, act-by passed,
             amount differs, no economics for the currency, policy version moved
             an earlier response whose outcome is unknown or was refused (a person
             reconciles it; nothing is resent), another run still uploading evidence,
             evidence missing from this dispute's index
refer       AUTO always; it records a case for a person and moves no money
close       AUTO when the provider status is terminal and the typed reason fits it (and, for
            ACCEPTED, the accept reason actually used); else BLOCKED

Policy is pure (runner rule 0): it reads the provider and ctx.store, never writes. It runs for
a proposal and for that run's rule-1 re-run, both before the run takes the dispute lock or
records an attempt, so every lock and attempt it sees is another run's (_case_blocks).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from awx_common import Money, RiskTier
from awx_common.workflow import PolicyContext, PolicyDecision

from .cases import (
    ANSWERED,
    AttemptState,
    attempt_state,
    attempts,
    case_fingerprint,
    case_value,
    lock_is_stale,
    next_attempt_number,
)
from .clearing import clear_blocks, recently_cleared
from .config import BAND_ORDER, DisputeConfig, StrengthBand
from .deadlines import ActionKind, Deadline, deadline_for
from .economics import Economics, Recommendation, UnsupportedCurrency, compute_economics
from .evidence import (
    EvidenceAssessment,
    EvidenceFile,
    Fact,
    FactCheck,
    assess,
    instruction_markers,
    minimum_evidence_gaps,
    resolve_evidence,
    verify_facts,
)
from .intents import facts_from_intent, money_from_intent
from .ledger import CLOSE_REASONS_FOR_STATUS, CloseReason, outcome_entries
from .provider import (
    CHALLENGE_REASON_FAMILIES,
    AcceptReason,
    ChallengeReason,
    DisputeView,
    ProviderShapeError,
    accept_request_id,
    challenge_request_id,
    dispute_read,
    items_of,
    parse_dispute,
    refunds_read,
)

ACCEPT_CLOSE_REASON: dict[AcceptReason, CloseReason] = {
    AcceptReason.LOW_VALUE: CloseReason.ACCEPTED_LOW_VALUE,
    AcceptReason.INSUFFICIENT_EVIDENCE: CloseReason.ACCEPTED_INSUFFICIENT_EVIDENCE,
    AcceptReason.MERCHANT_ERROR: CloseReason.ACCEPTED_MERCHANT_ERROR,
    AcceptReason.AFTER_ISSUER_REJECTION: CloseReason.ACCEPTED_AFTER_ISSUER_REJECTION,
}


def read_view(dispute_id: str, ctx: PolicyContext) -> DisputeView:
    return parse_dispute(ctx.read(dispute_read(dispute_id)))


def existing_refunds(view: DisputeView, ctx: PolicyContext) -> list[Mapping[str, Any]]:
    """Refunds on the disputed payment that did not fail. Unknown refund statuses count."""
    if view.payment_intent_id is None:
        return []
    items = items_of(ctx.read(refunds_read(view.payment_intent_id)))
    return [
        r
        for r in items
        if r.get("payment_intent_id") in (None, view.payment_intent_id)
        and str(r.get("status", "")).upper() not in {"FAILED", "CANCELLED"}
    ]


def _deadline_computed(deadline: Deadline) -> dict[str, Any]:
    return {
        "respond_by": deadline.respond_by,
        "deadline_source": deadline.source.value,
        "act_by": deadline.act_by,
        "approval_must_land_by": deadline.act_by,
    }


def _blocked(reasons: Sequence[str], computed: Mapping[str, Any] | None = None) -> PolicyDecision:
    return PolicyDecision(RiskTier.BLOCKED, tuple(reasons), dict(computed or {}))


def _common_blocks(
    intent: Mapping[str, Any], view: DisputeView, deadline: Deadline, config: DisputeConfig
) -> list[str]:
    blocks: list[str] = []
    if intent.get("policy_version") != config.policy_version:
        blocks.append("intent was validated under a different policy version")
    if not amount_matches(intent, view):
        blocks.append(f"proposal says {money_from_intent(intent)} but Airwallex says {view.amount}")
    if not view.is_respondable:
        blocks.append(f"dispute status {view.raw_status} does not accept a response")
    if deadline.passed:
        blocks.append(
            f"act-by time {deadline.act_by} has passed" if deadline.act_by else "no deadline"
        )
    if config.terms_for(view.amount.currency) is None:
        blocks.append(f"no dispute economics configured for {view.amount.currency}")
    return blocks


def _case_blocks(
    case: Mapping[str, Any],
    action: str,
    view: DisputeView,
    deadline: Deadline,
    ctx: PolicyContext,
    config: DisputeConfig,
) -> list[str]:
    """Every earlier attempt on this dispute, judged on what is known about it, so the reason
    the approver reads is accurate.

    Under the runners, policy runs for a proposal and once more before that run's first step
    (rule 1). Both happen before the run takes the dispute lock or records an attempt, so any
    lock or attempt seen here belongs to ANOTHER run. A proposal over another run's crashed or
    unresolved attempt is therefore refused here, at proposal time, and no approval is wasted
    on a run whose build could only fail closed."""
    blocks: list[str] = []
    due = f"respond by {deadline.respond_by}" if deadline.respond_by else "no known deadline"
    for a in attempts(case):
        state = attempt_state(case, a, view)
        kind = a["action"]
        if state is AttemptState.IN_FLIGHT:
            blocks.append(
                f"an earlier {kind} may have reached Airwallex (outcome unknown, or its run "
                f"never finished); a person reconciles it before any new response ({due}). "
                "Nothing is resent"
            )
        elif state is AttemptState.REFUSED:
            blocks.append(
                f"Airwallex refused the earlier {kind} at this stage; its request_id cannot "
                f"carry a new body, so a person decides ({due})"
            )
        elif (
            state is AttemptState.CLEARED
            and kind != action
            and a["decision"]["computed"].get("stage") == view.stage
            and recently_cleared(ctx, a)
        ):
            # Accept and challenge are irreversible, so the other action waits out the bound too.
            blocks.append(
                f"an earlier {kind} at this stage was cleared moments ago; a {action} waits "
                "until a late landing would be visible"
            )
        elif state is AttemptState.LANDED and kind == "accept":
            blocks.append("the dispute was already accepted")
        elif state is AttemptState.LANDED and action == "challenge":
            blocks.append("this dispute has already been challenged; no second challenge is sent")
        # LAPSED (refused, stage moved on) never reached Airwallex: it does not block. A
        # landed challenge
        # does not block an accept: that is the path after the issuer rejects the evidence.
    lock = case.get("lock")
    owner_has_attempt = lock is not None and any(
        a.get("owner") == lock.get("owner") for a in attempts(case)
    )
    if lock and not owner_has_attempt and not lock_is_stale(lock, ctx.now, config.upload_lock_ttl):
        blocks.append("another run is uploading evidence for this dispute; wait for it")
    if case.get("outcome"):
        blocks.append(f"the case is already closed as {case['outcome']['close_reason']}")
    return blocks


def _rid(derive, view: DisputeView, attempt: int = 1) -> str | None:
    """None at a stage this kit cannot answer; _common_blocks then blocks the proposal."""
    try:
        return derive(view.dispute_id, view.stage, attempt)
    except ProviderShapeError:
        return None


def _deadline_reasons(deadline: Deadline) -> list[str]:
    if deadline.source.value == "MODELLED":
        return [
            "DEADLINE IS MODELLED: Airwallex sent no due_at, so act-by is created_at plus the "
            "stage window; check the real deadline before approving"
        ]
    return []


def _dual(view: DisputeView, config: DisputeConfig) -> bool:
    terms = config.terms_for(view.amount.currency)
    return terms is not None and view.amount >= terms.dual_approval_threshold


def case_economics(
    view: DisputeView,
    evidence: Sequence[EvidenceFile],
    facts: Sequence[Fact],
    config: DisputeConfig,
    texts: Mapping[str, str] | None = None,
) -> tuple[FactCheck, EvidenceAssessment, Economics]:
    check = verify_facts(facts, evidence, texts)
    assessment = assess(view.reason_family, check.verified, config)
    econ = compute_economics(
        view.amount,
        assessment.band,
        config,
        merchant_fault=assessment.merchant_fault,
        contradictory=assessment.contradictory,
    )
    return check, assessment, econ


def accept_policy(
    intent: Mapping[str, Any], ctx: PolicyContext, config: DisputeConfig
) -> PolicyDecision:
    case = case_value(ctx.store, intent["dispute_id"])
    view = read_view(intent["dispute_id"], ctx)
    deadline = deadline_for(view, ActionKind.ACCEPT, config, ctx.now)
    computed: dict[str, Any] = {
        "dispute_id": view.dispute_id,
        "disputed_amount": view.amount,
        "reason_family": view.reason_family.value,
        "stage": view.stage,
        "response_attempt": next_attempt_number(case, "accept", view.stage),
        "request_id": _rid(
            accept_request_id, view, next_attempt_number(case, "accept", view.stage)
        ),
        "accept_reason": intent["reason"],
        "policy_version": config.policy_version,
        "case_state": case_fingerprint(case),
        **_deadline_computed(deadline),
    }
    blocks = _common_blocks(intent, view, deadline, config)
    blocks.extend(_case_blocks(case, "accept", view, deadline, ctx, config))
    try:
        econ = compute_economics(view.amount, StrengthBand.NONE, config)
    except UnsupportedCurrency:
        return _blocked(blocks, computed)
    computed.update(econ.to_computed())
    reason = AcceptReason(intent["reason"])
    if reason is AcceptReason.LOW_VALUE and econ.contest_can_beat_accept:
        blocks.append(
            f"LOW_VALUE needs fee plus handling ({econ.cost_contest_best}) to reach the cost of "
            f"accepting ({econ.cost_accept}); contesting could still pay here"
        )
    if reason is AcceptReason.AFTER_ISSUER_REJECTION and view.stage in (None, "RFI"):
        blocks.append("AFTER_ISSUER_REJECTION needs a dispute past RFI")
    refunds = existing_refunds(view, ctx)
    if refunds:
        blocks.append(
            f"{len(refunds)} refund(s) already exist on this payment; accepting would return "
            "the money twice"
        )
    if blocks:
        return _blocked(blocks, computed)
    reasons = [
        *_deadline_reasons(deadline),
        "accepting a dispute is irreversible: the disputed amount goes to the cardholder",
        f"typed accept reason {reason.value}",
        *econ.reasons,
    ]
    tier = RiskTier.DUAL if _dual(view, config) else RiskTier.SINGLE
    if tier is RiskTier.DUAL:
        reasons.append("disputed amount is at or above the dual-approval threshold")
    return PolicyDecision(tier, tuple(reasons), computed)


def challenge_policy(
    intent: Mapping[str, Any], ctx: PolicyContext, config: DisputeConfig
) -> PolicyDecision:
    case = case_value(ctx.store, intent["dispute_id"])
    view = read_view(intent["dispute_id"], ctx)
    deadline = deadline_for(view, ActionKind.CHALLENGE, config, ctx.now)
    indexed, evidence_problems = resolve_evidence(
        ctx.store, view.dispute_id, intent["evidence_ids"], config
    )
    evidence = [e.file for e in indexed]
    texts = {e.file.evidence_id: e.text for e in indexed if e.text}
    facts = facts_from_intent(intent)
    computed: dict[str, Any] = {
        "dispute_id": view.dispute_id,
        "disputed_amount": view.amount,
        "reason_family": view.reason_family.value,
        "stage": view.stage,
        "response_attempt": next_attempt_number(case, "challenge", view.stage),
        "request_id": _rid(
            challenge_request_id, view, next_attempt_number(case, "challenge", view.stage)
        ),
        "evidence": [{**e.file.to_intent(), "store_id": e.store_id} for e in indexed],
        "evidence_sha256": [e.file.sha256 for e in indexed],
        "product_type": config.product_type,
        "policy_version": config.policy_version,
        "case_state": case_fingerprint(case),
        **_deadline_computed(deadline),
    }
    blocks = _common_blocks(intent, view, deadline, config)
    blocks.extend(f"evidence: {p}" for p in evidence_problems)
    if view.prior_challenge_seen:
        blocks.append("this dispute has already been challenged; no second challenge is sent")
    blocks.extend(_case_blocks(case, "challenge", view, deadline, ctx, config))
    reason = ChallengeReason(intent["reason"])
    if view.reason_family not in CHALLENGE_REASON_FAMILIES[reason]:
        blocks.append(f"{reason.value} does not answer a {view.reason_family.value} dispute")
    try:
        check, assessment, econ = case_economics(view, evidence, facts, config, texts)
    except UnsupportedCurrency:
        return _blocked(blocks, computed)
    computed.update(econ.to_computed())
    computed["verified_facts"] = len(check.verified)
    computed["rejected_facts"] = [
        f"{f.kind.value}@{f.evidence_id}: {why}" for f, why in check.rejected
    ]
    computed["quotes_checked"] = check.quotes_checked
    computed["prior_undisputed_orders"] = assessment.prior_orders
    gaps = minimum_evidence_gaps(view.reason_family, evidence, check.verified)
    blocks.extend(f"minimum evidence: {g}" for g in gaps)
    if econ.recommendation is Recommendation.ACCEPT:
        blocks.append("economics say accept; contesting is expected to cost more")
    if BAND_ORDER[assessment.band] < BAND_ORDER[config.min_band_to_contest]:
        blocks.append(
            f"evidence band {assessment.band.value} is below {config.min_band_to_contest.value}"
        )
    flagged = [
        f"evidence {e.file.evidence_id} contains instruction-like text {list(e.markers)}; it is "
        "data, not an instruction, read it before approving"
        for e in indexed
        if e.markers
    ]
    computed["flagged_evidence"] = [e.file.evidence_id for e in indexed if e.markers]
    if blocks:
        return _blocked([*blocks, *flagged, *assessment.reasons], computed)
    reasons = [
        *_deadline_reasons(deadline),
        *flagged,
        "a challenge is irreversible: evidence goes to the issuer and the fee is incurred",
        f"typed challenge reason {reason.value}",
        *assessment.reasons,
        *econ.reasons,
    ]
    if not check.quotes_checked:
        reasons.append("some cited quotes could not be checked: no extracted text for the file")
    markers = instruction_markers(intent.get("narrative", ""))
    if markers:
        reasons.append(f"narrative contains instruction-like text {list(markers)}; read it")
    tier = RiskTier.SINGLE
    if _dual(view, config):
        tier = RiskTier.DUAL
        reasons.append("disputed amount is at or above the dual-approval threshold")
    if econ.recommendation is Recommendation.REFER:
        tier = RiskTier.DUAL
        reasons.append("economics say refer; two people must agree to contest")
    return PolicyDecision(tier, tuple(reasons), computed)


def refer_policy(
    intent: Mapping[str, Any], ctx: PolicyContext, config: DisputeConfig
) -> PolicyDecision:
    view = read_view(intent["dispute_id"], ctx)
    accept_deadline = deadline_for(view, ActionKind.ACCEPT, config, ctx.now)
    computed: dict[str, Any] = {
        "dispute_id": view.dispute_id,
        "disputed_amount": view.amount,
        "reason_family": view.reason_family.value,
        "status": view.raw_status,
        "policy_version": config.policy_version,
        **_deadline_computed(accept_deadline),
    }
    try:
        computed.update(compute_economics(view.amount, StrengthBand.NONE, config).to_computed())
    except UnsupportedCurrency:
        computed["economics"] = "no economics configured for this currency"
    reasons = ["referral records a case for a person; it moves no money"]
    if accept_deadline.passed:
        reasons.append("act-by time has passed; the person should expect the case to expire")
    return PolicyDecision(RiskTier.AUTO, tuple(reasons), computed)


def close_policy(
    intent: Mapping[str, Any], ctx: PolicyContext, config: DisputeConfig
) -> PolicyDecision:
    view = read_view(intent["dispute_id"], ctx)
    reason = CloseReason(intent["close_reason"])
    computed: dict[str, Any] = {
        "dispute_id": view.dispute_id,
        "status": view.raw_status,
        "close_reason": reason.value,
        "policy_version": config.policy_version,
    }
    case = case_value(ctx.store, view.dispute_id)
    recorded = case.get("outcome")
    if recorded and recorded.get("close_reason") != reason.value:
        return _blocked([f"the case is already closed as {recorded['close_reason']}"], computed)
    if intent.get("policy_version") != config.policy_version:
        return _blocked(["intent was validated under a different policy version"], computed)
    if not view.is_terminal:
        return _blocked([f"dispute status {view.raw_status} is not terminal"], computed)
    allowed = CLOSE_REASONS_FOR_STATUS.get(view.status, frozenset())
    if reason not in allowed:
        return _blocked(
            [f"{reason.value} does not describe a {view.status.value} dispute"], computed
        )
    used = [a["decision"]["computed"].get("accept_reason") for a in attempts(case, "accept")]
    if view.status.value == "ACCEPTED" and used:
        expected = {ACCEPT_CLOSE_REASON[AcceptReason(u)] for u in used if u}
        if expected and reason not in expected:
            names = sorted(e.value for e in expected)
            return _blocked(
                [f"the accept used {used}; close it as {names}, not {reason.value}"], computed
            )
    try:
        entries = outcome_entries(view, config)
    except ValueError as exc:
        return _blocked([str(exc)], computed)
    computed["ledger_entries"] = [
        {"kind": e.kind.value, "source": e.source.value, "amount": e.amount, "note": e.note}
        for e in entries
    ]
    return PolicyDecision(
        RiskTier.AUTO,
        (f"provider status {view.status.value}; recording outcome {reason.value}",),
        computed,
    )


def amount_matches(intent: Mapping[str, Any], view: DisputeView) -> bool:
    claimed: Money = money_from_intent(intent)
    return claimed.currency == view.amount.currency and claimed == view.amount


def clear_policy(
    intent: Mapping[str, Any], ctx: PolicyContext, config: DisputeConfig
) -> PolicyDecision:
    """A person clears a stuck response (the same rules as kit 8's clear). It acts only on a fresh
    read-back that is EMPTY for that response: the dispute still waits for a response at the
    attempt's own stage and shows no record of that action there. Anything undetermined (an
    unknown status, a stage move, a read that failed) is BLOCKED, and a response the dispute
    does show is LANDED, not cleared. Clearing keeps the attempt, so a late landing still reads
    LANDED and blocks a second response; the next response at that stage gets its own
    request_id. Never AUTO: one approver."""
    case = case_value(ctx.store, intent["dispute_id"])
    view = read_view(intent["dispute_id"], ctx)
    target = next((a for a in attempts(case) if a.get("request_id") == intent["request_ref"]), None)
    computed: dict[str, Any] = {
        "dispute_id": view.dispute_id,
        "status": view.raw_status,
        "stage": view.stage,
        "policy_version": config.policy_version,
        "case_state": case_fingerprint(case),
    }
    if intent.get("policy_version") != config.policy_version:
        return _blocked(["intent was validated under a different policy version"], computed)
    if target is None:
        return _blocked(["no response attempt with that request_id on this dispute"], computed)
    state = attempt_state(case, target, view)
    stage = target["decision"]["computed"].get("stage")
    computed.update(
        {
            "attempt_action": target["action"],
            "attempt_owner": target["owner"],
            "attempt_request_id": target["request_id"],
            "attempt_stage": stage,
            "attempt_state": state.value,
        }
    )
    if state not in (AttemptState.IN_FLIGHT, AttemptState.REFUSED):
        return _blocked([f"the attempt is {state.value}; there is nothing to clear"], computed)
    # Never while the original can still be sent or may not yet be visible.
    waits, deferred = clear_blocks(ctx, target)
    if waits:
        return _blocked(
            [f"not yet: {w}" for w in waits] + ["a person reconciles with Airwallex first"],
            computed,
        )
    empty = (
        view.is_respondable
        and view.stage == stage
        and not view.responded(target["action"], stage)
        and view.status not in ANSWERED[target["action"]]
    )
    if not empty:
        return _blocked(
            [
                f"the read-back is not empty (status {view.raw_status}, stage {view.stage}); "
                "a person reconciles with Airwallex first"
            ],
            computed,
        )
    return PolicyDecision(
        RiskTier.SINGLE,
        (
            *(
                ["the owner run's status is checked when the approved run starts"]
                if deferred
                else []
            ),
            f"Airwallex shows no {target['action']} at {stage}: clearing request_id "
            f"{target['request_id']} lets a new response be proposed under the next key",
            "the attempt is kept and watched: if it lands late, the case reads LANDED and a "
            "second response is refused by the kit (that Airwallex itself refuses a second "
            "response is FakeAirwallex behaviour, unconfirmed live)",
        ),
        computed,
    )
