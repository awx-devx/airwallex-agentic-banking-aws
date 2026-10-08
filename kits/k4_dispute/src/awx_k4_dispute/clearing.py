"""When a person may clear a stuck response.

A clear must never act while the original response can still be sent or may not yet be
visible at Airwallex. Both must hold, at proposal (once the run status is readable), at the
rule-1 recheck and again in the clear's postcondition:

- the run that owns the attempt reads terminal through `ctx.run_status(owner, owner action)`:
  RUN_RUNNING, None (no such run), an error, or no `run_status` callable at all is not proof;
- the attempt is older than CLEAR_BOUND: the attempt lease (a send in progress elsewhere), plus
  the response step's finality deadline, plus a provider-visibility margin.

With no callable at the agent's propose call (the tool plane has none and `ctx.proposal_id`
is None there) the decision is deferred to the recheck, where the callable exists; anywhere
else a missing callable BLOCKS.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from awx_common import transport
from awx_common.workflow import RUN_TERMINAL, PolicyContext

from .finality import ACCEPT_FINALITY, CHALLENGE_FINALITY

# awx_spine's DEFAULT_ATTEMPT_LEASE_S (the attempt lease): the kit may not import awx_spine,
# so a test pins the two values together.
ATTEMPT_LEASE_S = 180
VISIBILITY_MARGIN = timedelta(minutes=10)  # AWS-authored; dispute reads are not proven fresh
CLEAR_BOUND = (
    timedelta(seconds=ATTEMPT_LEASE_S)
    + timedelta(seconds=max(ACCEPT_FINALITY.timeout_s, CHALLENGE_FINALITY.timeout_s))
    + VISIBILITY_MARGIN
)
ROOT_ACTION = {"accept": "accept_dispute", "challenge": "challenge_dispute"}
DEFERRED = "DEFERRED"


def _when(value: Any) -> datetime | None:
    if isinstance(value, Mapping):
        value = transport.decode(value)
    return value if isinstance(value, datetime) else None


def owner_finished(ctx: PolicyContext, attempt: Mapping[str, Any]) -> str | None:
    """None when the owner's run reads terminal; otherwise why it is not proven finished, or
    DEFERRED at an agent propose call that has no run status yet."""
    check = ctx.run_status
    if check is None:
        if ctx.proposal_id is None:
            return DEFERRED
        return "the owner run's status cannot be read here"
    try:
        status = check(attempt["owner"], ROOT_ACTION[attempt["action"]])
    except Exception as exc:  # fail closed on any error
        return f"the owner run's status could not be read ({type(exc).__name__})"
    if status is None:
        return "no run is recorded for the attempt's owner"
    if status not in RUN_TERMINAL:
        return f"the owner run is {status}; its response may still be sent"
    return None


def too_recent(now: datetime, attempt: Mapping[str, Any]) -> str | None:
    recorded = _when(attempt.get("recorded_at"))
    if recorded is None:
        return "the attempt has no recorded time, so its age cannot be proven"
    age = now - recorded
    if age < CLEAR_BOUND:
        return (
            f"the attempt is {int(age.total_seconds())} s old; a clear waits "
            f"{int(CLEAR_BOUND.total_seconds())} s "
            "(attempt lease + finality deadline + visibility margin)"
        )
    return None


def clear_blocks(ctx: PolicyContext, attempt: Mapping[str, Any]) -> tuple[list[str], bool]:
    """(blocking reasons, deferred to recheck)."""
    reasons: list[str] = []
    age = too_recent(ctx.now, attempt)
    if age:
        reasons.append(age)
    owner = owner_finished(ctx, attempt)
    if owner == DEFERRED:
        return reasons, True
    if owner:
        reasons.append(owner)
    return reasons, False


def recently_cleared(ctx: PolicyContext, attempt: Mapping[str, Any]) -> bool:
    """A cleared attempt keeps blocking a different action at its stage for CLEAR_BOUND."""
    cleared = attempt.get("cleared") or {}
    at = _when(cleared.get("at"))
    return at is None or ctx.now - at < CLEAR_BOUND
