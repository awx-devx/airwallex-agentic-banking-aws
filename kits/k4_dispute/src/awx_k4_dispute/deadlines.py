"""Response deadlines.

The provider deadline wins when the read carries one. Otherwise the kit models one from
created_at and the stage's configured window and says so. The action must land before
act_by = deadline - margin, and that is checked twice: at proposal and again at execution, so
a deadline that passes while approval is pending stops the write.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from .config import DisputeConfig
from .provider import DisputeView


class DeadlineSource(StrEnum):
    PROVIDER = "PROVIDER"
    MODELLED = "MODELLED"
    NONE = "NONE"


class ActionKind(StrEnum):
    ACCEPT = "ACCEPT"
    CHALLENGE = "CHALLENGE"


class DeadlineMissed(Exception):
    """The act-by time has passed. Raised at execution; no write is built."""


@dataclass(frozen=True)
class Deadline:
    respond_by: datetime | None
    source: DeadlineSource
    act_by: datetime | None
    remaining: timedelta | None  # act_by - now; negative once passed

    @property
    def passed(self) -> bool:
        return self.act_by is None or self.remaining is None or self.remaining <= timedelta(0)


def respond_by(view: DisputeView, config: DisputeConfig) -> tuple[datetime | None, DeadlineSource]:
    if view.provider_deadline is not None:
        return view.provider_deadline, DeadlineSource.PROVIDER
    if view.created_at is not None:
        window = config.fallback_response_window.get(
            view.stage or "", config.default_fallback_window
        )
        return view.created_at + window, DeadlineSource.MODELLED
    return None, DeadlineSource.NONE


def deadline_for(
    view: DisputeView, action: ActionKind, config: DisputeConfig, now: datetime
) -> Deadline:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    due, source = respond_by(view, config)
    if due is None:
        return Deadline(None, source, None, None)
    margin = config.challenge_margin if action is ActionKind.CHALLENGE else config.accept_margin
    act_by = due - margin
    return Deadline(due, source, act_by, act_by - now)


def require_open(view: DisputeView, action: ActionKind, config: DisputeConfig, now: datetime):
    deadline = deadline_for(view, action, config, now)
    if deadline.passed:
        raise DeadlineMissed(
            f"act-by time {deadline.act_by} for {action.value} on {view.dispute_id} has passed"
            if deadline.act_by
            else f"no deadline can be established for {view.dispute_id}"
        )
    return deadline
