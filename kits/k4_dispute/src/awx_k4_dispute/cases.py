"""Case state in the kit store (`PolicyContext.store`): one record per dispute.

The record holds what the kit did on a dispute: the dispute lock, response attempts (accept or
challenge) with the decision each ran under, the responses the postcondition saw, referrals and
the recorded outcome. The kit's book id lives beside it (`book`, rule M2).

Runner rule 0: builds, postconditions and on_abandon may write here and must be idempotent.
Every write goes through update_case, which writes nothing when a change leaves the record as
it was, so a redelivered build or postcondition does not move the record's version.

The lock (rules M1 and M4)
- It is owned by the proposal_id of the run that took it (StepContext.proposal_id), never by
  the intent hash. The first build of an accept or challenge run takes it.
- Only its owner's postcondition releases it, and only on a definitive outcome: no response
  operation was sent, every one came back FAILED at create, or the dispute reads back in a
  responded state. After OUTCOME_UNKNOWN, a finality timeout, or a SUCCEEDED response the
  dispute does not yet show, the lock stays held and the response is recorded as in flight.
- A later run takes over a lock only when it has read a definitive outcome for the owner's
  attempt (landed), or when the owner never recorded an attempt (it only uploaded evidence,
  which moves no money) and the lock is older than `upload_lock_ttl`.
- on_abandon runs only when no step ran, so it never releases the lock.

Attempts
- An accept attempt is recorded with the lock, just before the accept is sent. A challenge
  attempt is recorded only once every upload succeeded and the challenge is about to be sent,
  so a failed upload leaves the case retryable (evidence uploads may be resent).
- `attempt_state` classifies each attempt as LANDED, REFUSED (every send FAILED at create) or
  IN_FLIGHT (anything else, including an attempt with no recorded result). Policy routes
  IN_FLIGHT and REFUSED to a person; a dispute endpoint does not deduplicate, so nothing is
  ever resent for a person to clean up (M1).
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from awx_common import intent_hash, transport
from awx_common.store import KitStore, StoredItem, VersionConflict
from awx_common.workflow import PolicyDecision

from .provider import DisputeStatus, DisputeView

_RETRIES = 5
BOOK_KEY = "book"


class LockHeld(Exception):
    """Another run owns the dispute lock and may still have a response in flight."""


class AttemptState(StrEnum):
    LANDED = "LANDED"  # the provider records this response at its stage
    REFUSED = "REFUSED"  # every send came back FAILED at create (definitive, M1)
    LAPSED = "LAPSED"  # refused at its stage, and the stage has since moved on: reopenable
    CLEARED = "CLEARED"  # a person cleared it after a fresh empty read-back (still watched)
    IN_FLIGHT = "IN_FLIGHT"  # anything else, including a run that never reached its postcondition


DEFINITIVE = frozenset(
    {AttemptState.LANDED, AttemptState.REFUSED, AttemptState.LAPSED, AttemptState.CLEARED}
)


ANSWERED: dict[str, frozenset[DisputeStatus]] = {
    "accept": frozenset({DisputeStatus.ACCEPTED, DisputeStatus.PENDING_CLOSURE}),
    "challenge": frozenset(
        {
            DisputeStatus.CHALLENGED,
            DisputeStatus.PENDING_DECISION,
            DisputeStatus.WON,
            DisputeStatus.REVERSED,
            DisputeStatus.LOST,
        }
    ),
}


def case_key(dispute_id: str) -> str:
    return f"case/{dispute_id}"


def get_case(store: KitStore | None, dispute_id: str) -> StoredItem | None:
    return None if store is None else store.get(case_key(dispute_id))


def case_value(store: KitStore | None, dispute_id: str) -> Mapping[str, Any]:
    item = get_case(store, dispute_id)
    return {} if item is None else item.value


def update_case(
    store: KitStore | None,
    dispute_id: str,
    change: Callable[[dict[str, Any]], dict[str, Any]],
) -> StoredItem | None:
    """Read-modify-write with optimistic concurrency. A no-op change writes nothing."""
    if store is None:
        return None
    for _ in range(_RETRIES):
        item = store.get(case_key(dispute_id))
        before = dict(item.value) if item else {"dispute_id": dispute_id}
        after = change(transport.loads(transport.dumps(before)))
        if item is not None and transport.dumps(after) == transport.dumps(item.value):
            return item
        try:
            return store.put(
                case_key(dispute_id),
                after,
                expected_version=None if item is None else item.version,
            )
        except VersionConflict:
            continue
    raise VersionConflict(f"case {dispute_id} kept moving; giving up after {_RETRIES} tries")


# ---- book id (rule M2) ---------------------------------------------------------------------


BOOK_ID = re.compile(r"^bk_[0-9a-f]{16}$")


def check_book_id(book_id: object) -> str:
    """Check the whole book id, not just its prefix."""
    if not isinstance(book_id, str) or not BOOK_ID.match(book_id):
        raise ValueError("pass the kit's book id from cases.ensure_book (bk_ + 16 hex)")
    return book_id


def ensure_book(store: KitStore) -> str:
    """The kit's book id for this tenant: minted once, at random, with a conditional create,
    and stored with the kit's state. Keys built from kit-local values carry it."""
    item = store.get(BOOK_KEY)
    if item is not None:
        return check_book_id(item.value["book_id"])
    try:
        store.put(BOOK_KEY, {"book_id": f"bk_{secrets.token_hex(8)}"}, expected_version=None)
    except VersionConflict:
        pass
    return check_book_id(store.get(BOOK_KEY).value["book_id"])


# ---- attempts ------------------------------------------------------------------------------


def intent_digest(intent: Mapping[str, Any]) -> str:
    return intent_hash(intent)


def attempts(case: Mapping[str, Any], action: str | None = None) -> list[Mapping[str, Any]]:
    return [a for a in case.get("attempts", []) if action is None or a.get("action") == action]


def attempt_of(case: Mapping[str, Any], action: str, owner: str) -> Mapping[str, Any] | None:
    """The attempt this run (its proposal_id) recorded for action, if any (M4)."""
    return next((a for a in attempts(case, action) if a.get("owner") == owner), None)


def own_attempt(
    case: Mapping[str, Any], action: str, intent: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    digest = intent_digest(intent)
    return next((a for a in attempts(case, action) if a.get("intent_digest") == digest), None)


def response_for(case: Mapping[str, Any], attempt: Mapping[str, Any]) -> Mapping[str, Any] | None:
    return next(
        (
            r
            for r in case.get("responses", [])
            if r.get("owner") == attempt.get("owner")
            and r.get("intent_digest") == attempt.get("intent_digest")
        ),
        None,
    )


def attempt_state(
    case: Mapping[str, Any], attempt: Mapping[str, Any], view: DisputeView
) -> AttemptState:
    """Judged from this stage's own record, never from a stage change
    alone. The provider's response record at the attempt's stage, or the dispute in a responded
    state at that stage, is LANDED. A response Airwallex refused at its stage, once the stage
    has moved on, is LAPSED (a refused RFI that ran out into a chargeback re-opens the case for
    the new stage). Anything unknown stays IN_FLIGHT, with a person."""
    action = attempt["action"]
    stage = attempt["decision"]["computed"].get("stage")
    if view.responded(action, stage):
        return AttemptState.LANDED
    if view.status in ANSWERED[action] and (stage is None or view.stage == stage):
        return AttemptState.LANDED
    if attempt.get("cleared"):  # watched: a late landing above still reads LANDED
        return AttemptState.CLEARED
    response = response_for(case, attempt)
    state = response.get("state") if response else None
    if state == AttemptState.LANDED:
        return AttemptState.LANDED
    if state == AttemptState.REFUSED:
        moved = stage is not None and view.stage is not None and view.stage != stage
        return AttemptState.LAPSED if moved else AttemptState.REFUSED
    # IN_FLIGHT, or no response record (a run lost before its postcondition): a stage change
    # is NOT proof it never landed while the per-stage record shape is unconfirmed live,
    # so it stays with a person.
    return AttemptState.IN_FLIGHT


def next_attempt_number(case: Mapping[str, Any], action: str, stage: str | None) -> int:
    """The response attempt number for a new proposal at this stage (request_id segment). Any
    earlier attempt at the same stage that is not cleared blocks the proposal anyway."""
    same = [a for a in attempts(case, action) if a["decision"]["computed"].get("stage") == stage]
    return len(same) + 1


def clear_attempt(
    store: KitStore | None,
    dispute_id: str,
    owner: str,
    request_id: str,
    cleared_by: str,
    *,
    at: datetime,
    owner_terminal: bool,
) -> None:
    """A person cleared `owner`'s attempt after a fresh empty read-back: mark it, keep it (so a
    late landing is still seen). The lock is released only when that attempt's run holds it AND
    the caller has read that run terminal; otherwise it stays."""

    def change(case: dict[str, Any]) -> dict[str, Any]:
        for a in case.get("attempts", []):
            if a.get("owner") == owner and a.get("request_id") == request_id:
                if not a.get("cleared"):
                    a["cleared"] = {"by": cleared_by, "at": at}
        if owner_terminal and (lock_of(case) or {}).get("owner") == owner:
            case.pop("lock", None)
        return case

    update_case(store, dispute_id, change)


# ---- the lock ------------------------------------------------------------------------------


def lock_of(case: Mapping[str, Any]) -> Mapping[str, Any] | None:
    return case.get("lock") or None


def _lock_free_for(
    case: Mapping[str, Any], owner: str, view: DisputeView, now: datetime, ttl: timedelta
) -> bool:
    lock = lock_of(case)
    if lock is None or lock.get("owner") == owner:
        return True
    held = [a for a in attempts(case) if a.get("owner") == lock.get("owner")]
    if held:  # M4: take over only after reading a definitive outcome for the owner's response
        return all(attempt_state(case, a, view) in DEFINITIVE for a in held)
    return lock_is_stale(lock, now, ttl)


def lock_is_stale(lock: Mapping[str, Any], now: datetime, ttl: timedelta) -> bool:
    """An upload-only lock (no response attempt, no money) older than ttl."""
    taken = lock.get("taken_at")
    if isinstance(taken, Mapping):
        taken = transport.decode(taken)
    return isinstance(taken, datetime) and now - taken >= ttl


def take_lock(
    store: KitStore | None,
    dispute_id: str,
    owner: str,
    action: str,
    view: DisputeView,
    now: datetime,
    ttl: timedelta,
) -> None:
    """Take or keep the dispute lock for `owner`. Idempotent for the owner."""

    def change(case: dict[str, Any]) -> dict[str, Any]:
        if not _lock_free_for(case, owner, view, now, ttl):
            raise LockHeld(f"dispute {dispute_id} is locked by run {case['lock']['owner']}")
        lock = lock_of(case)
        if lock is None or lock.get("owner") != owner:
            case["lock"] = {"owner": owner, "action": action, "taken_at": now}
        return case

    update_case(store, dispute_id, change)


def record_attempt(
    store: KitStore | None,
    dispute_id: str,
    owner: str,
    action: str,
    intent: Mapping[str, Any],
    decision: PolicyDecision,
    request_id: str,
    now: datetime | None = None,
) -> None:
    """One attempt per action and run (proposal_id), recorded by the lock owner just before the
    send. Idempotent: a redelivered build writes nothing."""
    digest = intent_digest(intent)

    def change(case: dict[str, Any]) -> dict[str, Any]:
        lock = lock_of(case)
        if lock is None or lock.get("owner") != owner:
            raise LockHeld(f"run {owner} does not hold the lock on {dispute_id}")
        if any(
            a.get("action") == action and a.get("owner") == owner for a in case.get("attempts", [])
        ):
            return case
        case["attempts"] = [
            *case.get("attempts", []),
            {
                "action": action,
                "intent_digest": digest,
                "owner": owner,
                "request_id": request_id,
                "recorded_at": now,
                "decision": {
                    "risk_tier": decision.risk_tier.value,
                    "reasons": list(decision.reasons),
                    "computed": dict(decision.computed),
                },
            },
        ]
        return case

    update_case(store, dispute_id, change)


def finish(
    store: KitStore | None,
    dispute_id: str,
    owner: str,
    response: Mapping[str, Any] | None,
    *,
    release: bool,
) -> None:
    """The owner's postcondition: record its response (one per owner and intent) and release
    the lock only when `release` (a definitive outcome). Never touches another run's lock."""

    def change(case: dict[str, Any]) -> dict[str, Any]:
        if response is not None:
            key = (response["owner"], response["intent_digest"])
            rest = [
                r
                for r in case.get("responses", [])
                if (r.get("owner"), r.get("intent_digest")) != key
            ]
            case["responses"] = [*rest, dict(response)]
        if release and (lock_of(case) or {}).get("owner") == owner:
            case.pop("lock", None)
        return case

    update_case(store, dispute_id, change)


def case_fingerprint(case: Mapping[str, Any]) -> str:
    """What a pending approval binds to: attempts and the outcome. Referrals, responses and the
    lock are left out, so recording a referral does not invalidate a pending challenge."""
    return intent_hash(
        {
            "attempts": sorted(
                f"{a.get('action')}:{a.get('intent_digest')}" for a in case.get("attempts", [])
            ),
            "outcome": (case.get("outcome") or {}).get("close_reason"),
        }
    )
