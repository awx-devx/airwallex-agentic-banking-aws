"""propose_release_obligation: the person-approved release tool for a stuck obligation lock.

Locks go to NEEDS_PERSON (an unknown outcome, converted but not paid, a cancel after a
conversion), stay SUBMITTED when the provider never shows an accepted transfer, or stay LOCKED
when a run died. Only a person may move them on, and only from evidence this policy reads:

| evidence for the lock's current attempt                         | action    | tier   |
|-----------------------------------------------------------------|-----------|--------|
| the transfer reads PAID                                         | MARK_PAID | SINGLE |
| the transfer reads CANCELLED with its failure block             | RELEASE   | SINGLE |
| the transfer reads SCHEDULED, PROCESSING or SENT                | none      | BLOCKED|
| nothing found, and the submission ledger shows nothing emitted  | RELEASE   | SINGLE |
| nothing found, and this attempt's only send was refused at create| RELEASE   | SINGLE |
| nothing found, emitted, past the dedup window (M1)              | RELEASE   | DUAL   |
| nothing found, emitted, inside the window                       | none      | BLOCKED|
| the provider object does not match, or a read fails             | none      | BLOCKED|
| the lock owner's execute_plan run is not known to be terminal    | none      | BLOCKED|
| past the window, but a matching transfer is listed since the lock | none      | BLOCKED|

A LOCKED lock may belong to a run that is still building. Safety does not rest on time: every
build records its submission (and a per-obligation emission index) BEFORE a CAS claim on the
lock, and operator_set is a CAS on the same lock. A release that reads the ledger after the
record sees the emission and is BLOCKED inside the dedup window; one that read before it binds
a lock version the claim then moves (LockMoved); one whose write lands before the claim makes
the claim raise, so nothing is sent. On top of that, nothing is released
while the lock owner's execute_plan run may still send anything: the policy reads
`ctx.run_status(<owner proposal_id>, "execute_plan")` and releases only on a
member of RUN_TERMINAL. RUN_RUNNING, None (no such run), an error, or a None callable at
recheck all BLOCK. At propose time the tool plane has no run status (None callable): the
decision is deferred to rule 1's recheck, which BLOCKS (the hash moves, STALE) unless the run is
terminal. The apply step reads it once more. There is no time fallback.

The DUAL window is measured from the transfer's first submission record (`recorded_at`).
That is sound because kit 1 re-emits a transfer only inside one run (rule 9 resends, minutes
apart); a cross-run re-emission (allowed by M1) would need a `last_emitted_at` field.

RELEASE keeps the attempt, so the next run retakes the lock at attempt + 1 with new
request_ids (M1); releasing after a conversion is safe because the converted money sits in its
wallet and the next plan converts less. The approval binds the lock's store version, so a lock
that moved after the proposal fails the step closed (LockMoved). The model may propose this,
but never below SINGLE, and the reason it gives is only recorded.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, timedelta
from typing import Any

from awx_common import Operation, ReadRequest, RiskTier
from awx_common.workflow import (
    RUN_TERMINAL,
    KitWorkflowSpec,
    PolicyContext,
    PolicyDecision,
    PostconditionResult,
    ReconcileUndetermined,
    StepContext,
    StepSpec,
)

from .api import (
    FX_CONVERSIONS,
    READ_ALLOWLIST,
    TRANSFERS,
    find_transfer,
)
from .book import NO_BOOK, load_book, load_book_id
from .intake import InvalidInput, require_keys
from .keys import KIT, obligation_key, transfer_body, transfer_request_id
from .locks import (
    NOTE_REFUSED,
    LockState,
    emissions,
    get_submission,
    need_store,
    operator_set,
    read_lock,
)
from .money_rules import BadProviderData
from .policy import PolicyConfig

ACTION = "release_obligation"


class OwnerRunLive(Exception):
    """The lock owner's execute_plan run is not known to be terminal; nothing is released."""


def owner_run_gate(ctx, owner: str) -> str | None:
    """None when the owner's execute_plan run can send nothing more, else why not.

    At propose time (ctx.proposal_id is None) with no run_status callable, defer: return None
    and let rule 1's recheck decide. Anywhere else a missing callable BLOCKS."""
    lookup = getattr(ctx, "run_status", None)
    if lookup is None:
        if getattr(ctx, "proposal_id", None) is None:
            return None  # propose time: decide at recheck
        return "no run status available; the owner run cannot be shown to be finished"
    try:
        status = lookup(owner, "execute_plan")
    except Exception as exc:  # throttling, a malformed id, a child's id: fail closed
        return f"run status of {owner} unreadable ({type(exc).__name__}: {exc})"[:300]
    if status in RUN_TERMINAL:
        return None
    if status is None:
        return f"no execute_plan run {owner} is known; cannot show it is finished"
    return f"the owner run {owner} is {status}; it may still send"


def _listed(read, path: str, query: Mapping[str, str]) -> list[Mapping[str, Any]]:
    items = read(ReadRequest(path, dict(query))).get("items")
    return [i for i in items if isinstance(i, Mapping)] if isinstance(items, list) else []


MAX_PAGES = 20
PAGE_SIZE = 100


# At propose time the gate defers, so an approver must not read the
# tier as clearance. The approved run re-checks the owner run (rule 1) and again at apply.
OWNER_RUN_CHECKED_AT_EXECUTION = (
    "the lock owner's execute_plan run status is checked when the approved run starts and "
    "again at apply; if it may still send, the release ends STALE or BLOCKED and writes nothing"
)


class TooManyPages(Exception):
    pass


def _all_pages(read, path: str, since=None) -> list[Mapping[str, Any]]:
    """Every row of a list, page by page, from `since` when given (`from_created_at`; the
    sandbox accepted it in the %Y-%m-%dT%H:%M:%S+0000 form and filtered on it, live 6 Oct).
    Raises TooManyPages past MAX_PAGES, on a page without an items list, or on a `has_more`
    that is present but not a bool.

    The live sandbox OMITS `has_more` on GET /transfers (6 Oct, evidence/live-run-1.json).
    Without it the scan stops only on an EMPTY page (one extra read), so a server that caps or
    filters pages below PAGE_SIZE cannot end it early; a page that repeats an id already seen
    means page_num is not honoured, and BLOCKS. Up to MAX_PAGES full pages are read; one more
    read (page MAX_PAGES) may only confirm the end, so the cap is exactly MAX_PAGES * PAGE_SIZE
    rows."""
    out: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    base = {"page_size": str(PAGE_SIZE)}
    if since is not None:
        base["from_created_at"] = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+0000")
    for num in range(MAX_PAGES + 1):
        body = read(ReadRequest(path, {**base, "page_num": str(num)}))
        items = body.get("items")
        if not isinstance(items, list):
            raise TooManyPages(f"{path} page {num} has no items list")
        rows = [i for i in items if isinstance(i, Mapping)]
        ids = [str(r.get("id")) for r in rows]
        if seen.intersection(ids):
            raise TooManyPages(f"{path} page {num} repeats rows already seen; paging not honoured")
        if num == MAX_PAGES:
            if rows:
                break  # a row past the cap: the scan cannot finish
            return out
        seen.update(ids)
        out.extend(rows)
        more = body.get("has_more")
        if more is None:
            more = bool(rows)  # absent: only an empty page ends the scan
        elif not isinstance(more, bool):
            raise TooManyPages(f"{path} page {num} has a non-boolean has_more")
        if not more:
            return out
    raise TooManyPages(f"{path} has more than {MAX_PAGES * PAGE_SIZE} rows")


def build_release_spec(config: PolicyConfig | None = None) -> KitWorkflowSpec:
    cfg = config or PolicyConfig()

    def validate(raw: Mapping[str, Any]) -> Mapping[str, Any]:
        require_keys(raw, {"obligation_id", "reason"}, "propose_release_obligation input")
        oid, reason = raw.get("obligation_id"), raw.get("reason")
        if not isinstance(oid, str) or not 1 <= len(oid) <= 64:
            raise InvalidInput("obligation_id is required")
        if not isinstance(reason, str) or not 10 <= len(reason.strip()) <= 500:
            raise InvalidInput("reason must be 10 to 500 characters")
        return {"obligation_id": oid, "reason": reason.strip()}

    def blocked(why: str) -> PolicyDecision:
        return PolicyDecision(RiskTier.BLOCKED, (why,))

    def policy(intent: Mapping[str, Any], ctx: PolicyContext) -> PolicyDecision:
        oid = intent["obligation_id"]
        book = load_book(ctx.store)
        book_id = load_book_id(ctx.store)
        if book is None or not book_id:
            return blocked(NO_BOOK)
        # Read the lock BEFORE the submission. A build records first and claims second, so with
        # this order a build that lands between the two reads either shows its record (BLOCK)
        # or moves the lock version we bind (operator_set then loses, LockMoved). Read the
        # other way round, a release would rely on rule 1's recheck before the apply step to
        # catch it (test_a_build_between_the_release_reads_is_caught_by_rule_1 pins that the
        # recheck does).
        lock = read_lock(ctx.store, oid) if ctx.store else None
        if lock is None or lock.state in (LockState.RELEASED, LockState.PAID):
            return blocked(
                f"{oid} has nothing to release ({lock.state.value if lock else 'no lock'})"
            )
        rid = transfer_request_id(book_id, oid, lock.attempt)
        sub = get_submission(ctx.store, rid)
        if sub is not None:
            body = dict(sub["body"])
        else:
            try:
                ob = book.obligation(oid)
            except KeyError:
                return blocked(f"{oid} is not in the book")
            body = transfer_body(
                {
                    "obligation_id": oid,
                    "beneficiary_id": ob.beneficiary_id,
                    "amount": ob.amount,
                    "method": ob.method.value,
                },
                rid,
                book_id,
            )
        probe = Operation(
            kit=KIT,
            name="transfers.create",
            method="POST",
            path="/api/v1/transfers/create",
            body=body,
            request_id=rid,
            business_key=obligation_key(book_id, oid),
            intent_hash="0" * 64,
        )
        try:
            found = find_transfer(probe, ctx.read)
        except (ReconcileUndetermined, BadProviderData) as exc:
            return blocked(f"cannot tell what the provider holds for {rid}: {exc}")
        # What an approver needs. Only facts that do not move with the clock go in
        # computed (it is in the approval hash); ages go in the reasons.
        since = lock.claimed_at or lock.locked_at
        age = ctx.now - since if since is not None else None
        age_text = f"lock age {age}" if age is not None else "lock age unknown"
        evidence: dict[str, Any] = {
            "transfer_request_id": rid,
            "emitted": sub is not None,
            "lock_owner": lock.owner,
            "lock_note": lock.note,
            "lock_claims": lock.claims,
            "transfer": {
                "beneficiary_id": body["beneficiary_id"],
                "amount": body["transfer_amount"],
                "currency": body["transfer_currency"],
                "reference": body["reference"],
            },
            "conversions": [],
        }
        emitted = emissions(ctx.store, oid, lock.attempt)
        evidence["emissions"] = sorted((e["kind"], e["request_id"]) for e in emitted)
        conv_rids = sorted(
            set(lock.conversion_rids)
            | {e["request_id"] for e in emitted if e["kind"] == "conversion"}
        )
        for crid in conv_rids:
            got = _listed(ctx.read, FX_CONVERSIONS, {"request_id": crid})
            evidence["conversions"].append(
                {
                    "request_id": crid,
                    "recorded": get_submission(ctx.store, crid) is not None,
                    "provider": [
                        {
                            "status": g.get("status"),
                            "sell_amount": g.get("sell_amount"),
                            "quote_id": g.get("quote_id"),
                        }
                        for g in got
                    ],
                }
            )
        # A recorded conversion of this attempt that the provider does not show
        # may still land. Inside the dedup window that BLOCKS; past it, a release needs DUAL.
        unseen = [
            (c["request_id"], (get_submission(ctx.store, c["request_id"]) or {}).get("recorded_at"))
            for c in evidence["conversions"]
            if c["recorded"] and not c["provider"]
        ]
        live = owner_run_gate(ctx, lock.owner)
        if live is not None:
            return blocked(f"{oid} is {lock.state.value} under {lock.owner} ({age_text}): {live}")
        if found is not None:
            status = str(found.get("status", ""))
            evidence["provider_status"] = status
            if status == "PAID":
                action, tier = "MARK_PAID", RiskTier.SINGLE
            elif status == "CANCELLED" and isinstance(found.get("failure"), Mapping):
                action, tier = "RELEASE", RiskTier.SINGLE
            else:
                return blocked(f"{oid}'s transfer is {status or 'unknown'}; wait for it to finish")
        elif unseen and any(t is None or ctx.now - t <= cfg.dedup_window for _, t in unseen):
            return blocked(
                f"{oid}: conversion {[r for r, _ in unseen]} was recorded and is not visible at "
                f"the provider; it may still land. Wait for it or for the "
                f"{cfg.dedup_window.days}-day window to pass"
            )
        elif sub is None:
            # No transfer was ever recorded under this attempt (conversions are judged just
            # above). A recorded conversion past the window still needs DUAL.
            action = "RELEASE"
            tier = RiskTier.DUAL if unseen else RiskTier.SINGLE
        elif NOTE_REFUSED in lock.note and not unseen:
            # This attempt's only send was refused at create (definitive, M1): nothing exists.
            evidence["refused_at_create"] = True
            action, tier = "RELEASE", RiskTier.SINGLE
        else:
            since = sub.get("recorded_at") or lock.submitted_at
            if since is None or ctx.now - since <= cfg.dedup_window:
                return blocked(
                    f"{oid}'s transfer may still land under {rid}; wait for the provider or for "
                    f"the {cfg.dedup_window.days}-day window to pass"
                )
            # Cross-check the list by beneficiary and reference since the lock was
            # taken, not only the request_id read; any non-CANCELLED match blocks.
            try:
                every = _all_pages(ctx.read, TRANSFERS, lock.locked_at)
            except TooManyPages as exc:
                return blocked(f"{oid}: cannot cross-check every transfer ({exc}); not released")
            listed = [
                t
                for t in every
                if t.get("reference") == body["reference"]
                and t.get("beneficiary_id") == body["beneficiary_id"]
                and t.get("status") != "CANCELLED"
                and _created_since(t, lock.locked_at)
            ]
            evidence["searched"] = (
                f"GET /transfers?request_id={rid}, and GET /transfers for reference "
                f"{body['reference']} and beneficiary {body['beneficiary_id']} since the lock"
            )
            if listed:
                return blocked(
                    f"{oid}: {len(listed)} matching transfer(s) listed since the lock "
                    f"({[t.get('id') for t in listed]}); not released"
                )
            action, tier = "RELEASE", RiskTier.DUAL
        computed = {
            "obligation_id": oid,
            "lock_state": lock.state.value,
            "lock_attempt": lock.attempt,
            "lock_store_version": lock.version,
            "action": action,
            "evidence": evidence,
        }
        why = f"{action} {oid}: {lock.state.value} (owner {lock.owner}, {age_text}), {evidence}"
        return PolicyDecision(tier, (why, OWNER_RUN_CHECKED_AT_EXECUTION), computed)

    def build_apply(ctx: StepContext) -> Sequence[Operation]:
        c = ctx.decision.computed
        state = LockState.PAID if c["action"] == "MARK_PAID" else LockState.RELEASED
        note = f"{c['action']} by {ctx.proposal_id}: {ctx.intent['reason']}"[:300]
        store = need_store(ctx.policy.store)
        live = owner_run_gate(ctx.policy, c["evidence"]["lock_owner"])
        if live is not None or ctx.policy.run_status is None:
            raise OwnerRunLive(live or "no run status at apply")  # re-checked at apply
        done = read_lock(store, c["obligation_id"])
        if done is not None and done.state is state and done.note == note:
            return ()  # rule 0: a second build of this release finds it already applied
        operator_set(
            store,
            c["obligation_id"],
            expected_store_version=c["lock_store_version"],
            state=state,
            note=note,
        )
        return ()  # a store-only step: nothing is sent to Airwallex

    def postcondition(ctx: StepContext) -> PostconditionResult:
        c = ctx.decision.computed
        lock = read_lock(need_store(ctx.policy.store), c["obligation_id"])
        want = LockState.PAID if c["action"] == "MARK_PAID" else LockState.RELEASED
        if lock is None or lock.state is not want:
            return PostconditionResult(False, f"lock is {lock.state.value if lock else 'gone'}")
        return PostconditionResult(
            True, f"{c['obligation_id']} {want.value} at attempt {lock.attempt}"
        )

    return KitWorkflowSpec(
        kit=KIT,
        action=ACTION,
        validate=validate,
        policy=policy,
        steps=(StepSpec("apply", build_apply),),
        approval_ttl=cfg.approval_ttl,
        read_allowlist=READ_ALLOWLIST,
        postcondition=postcondition,
        # default agent floor (SINGLE); policy never returns AUTO
    )


def _created_since(obj: Mapping[str, Any], since) -> bool:
    from datetime import datetime

    if since is None:
        return True
    raw = obj.get("created_at")
    if not isinstance(raw, str):
        return True  # unknown: count it, so the check fails closed
    if len(raw) > 5 and raw[-5] in "+-" and raw[-4:].isdigit():
        raw = raw[:-2] + ":" + raw[-2:]
    try:
        created = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return True
    return created.tzinfo is None or created >= since - timedelta(minutes=5)
