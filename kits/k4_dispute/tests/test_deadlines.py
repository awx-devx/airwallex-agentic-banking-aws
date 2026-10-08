"""Deadlines and provider parsing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from awx_k4_dispute.config import DEFAULT_CONFIG
from awx_k4_dispute.deadlines import (
    ActionKind,
    DeadlineMissed,
    DeadlineSource,
    deadline_for,
    require_open,
)
from awx_k4_dispute.provider import (
    DisputeStatus,
    ProviderShapeError,
    ReasonFamily,
    parse_dispute,
    parse_time,
)
from hypothesis import given
from hypothesis import strategies as st

NOW = datetime(2026, 10, 6, 4, 0, tzinfo=UTC)


def body(**over):
    b = {
        "id": "dsp_1",
        "amount": "100.00",
        "currency": "AUD",
        "reason": {"code": "10.4"},
        "stage": "RFI",
        "status": "REQUIRES_RESPONSE",
        "created_at": "2026-10-05T04:00:00Z",
    }
    b.update(over)
    return b


def test_provider_deadline_wins_and_margin_applies():
    view = parse_dispute(body(due_at="2026-10-10T00:00:00+0000"))
    d = deadline_for(view, ActionKind.CHALLENGE, DEFAULT_CONFIG, NOW)
    assert d.source is DeadlineSource.PROVIDER
    assert d.respond_by == datetime(2026, 10, 10, tzinfo=UTC)
    assert d.act_by == datetime(2026, 10, 9, tzinfo=UTC)
    a = deadline_for(view, ActionKind.ACCEPT, DEFAULT_CONFIG, NOW)
    assert a.act_by == datetime(2026, 10, 9, 23, tzinfo=UTC)


def test_missing_provider_deadline_is_modelled_from_stage_window():
    view = parse_dispute(body())
    d = deadline_for(view, ActionKind.ACCEPT, DEFAULT_CONFIG, NOW)
    assert d.source is DeadlineSource.MODELLED
    assert d.respond_by == datetime(2026, 10, 10, 4, tzinfo=UTC)  # created + 5 days at RFI


def test_no_dates_at_all_counts_as_passed():
    view = parse_dispute(body(created_at=None))
    d = deadline_for(view, ActionKind.ACCEPT, DEFAULT_CONFIG, NOW)
    assert d.source is DeadlineSource.NONE
    assert d.passed
    with pytest.raises(DeadlineMissed):
        require_open(view, ActionKind.ACCEPT, DEFAULT_CONFIG, NOW)


def test_naive_now_is_refused():
    with pytest.raises(ValueError, match="aware"):
        deadline_for(
            parse_dispute(body()), ActionKind.ACCEPT, DEFAULT_CONFIG, NOW.replace(tzinfo=None)
        )


@given(
    due_offset=st.integers(min_value=-10 * 24 * 60, max_value=10 * 24 * 60),
    action=st.sampled_from(list(ActionKind)),
)
def test_act_by_is_due_minus_margin_and_passed_iff_now_reaches_it(due_offset, action):
    due = NOW + timedelta(minutes=due_offset)
    view = parse_dispute(body(due_at=due.isoformat()))
    d = deadline_for(view, action, DEFAULT_CONFIG, NOW)
    margin = (
        DEFAULT_CONFIG.challenge_margin
        if action is ActionKind.CHALLENGE
        else DEFAULT_CONFIG.accept_margin
    )
    assert d.act_by == due - margin
    assert d.passed == (NOW >= due - margin)
    if d.passed:
        with pytest.raises(DeadlineMissed):
            require_open(view, action, DEFAULT_CONFIG, NOW)
    else:
        assert require_open(view, action, DEFAULT_CONFIG, NOW).act_by == d.act_by


@given(hours=st.integers(min_value=0, max_value=240))
def test_challenge_window_closes_no_later_than_accept_window(hours):
    view = parse_dispute(body(due_at=(NOW + timedelta(hours=hours)).isoformat()))
    c = deadline_for(view, ActionKind.CHALLENGE, DEFAULT_CONFIG, NOW)
    a = deadline_for(view, ActionKind.ACCEPT, DEFAULT_CONFIG, NOW)
    assert c.act_by <= a.act_by
    assert not (a.passed and not c.passed)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-10-06T04:00:00Z", datetime(2026, 10, 6, 4, tzinfo=UTC)),
        ("2026-10-06T14:00:00+1000", datetime(2026, 10, 6, 4, tzinfo=UTC)),
        ("2026-10-06T04:00:00", datetime(2026, 10, 6, 4, tzinfo=UTC)),
        ("not a date", None),
        (None, None),
        (1234, None),
    ],
)
def test_parse_time(text, expected):
    assert parse_time(text) == expected


def test_parse_dispute_normalises_and_fails_closed():
    view = parse_dispute(body(status="SOMETHING_NEW", reason_code="13.6", reason=None))
    assert view.status is DisputeStatus.UNKNOWN
    assert not view.is_respondable
    assert not view.is_terminal
    assert view.reason_family is ReasonFamily.CREDIT_NOT_PROCESSED
    assert parse_dispute(body(reason={"code": "99.9"})).reason_family is ReasonFamily.UNKNOWN
    assert parse_dispute(body(challenges=[{"id": "c1"}])).prior_challenge_seen


@pytest.mark.parametrize(
    "bad",
    [
        {"amount": 100.5},
        {"amount": True},
        {"amount": None},
        {"currency": None},
        {"id": ""},
        {"amount": "1.234"},
    ],
)
def test_parse_dispute_refuses_floats_and_missing_fields(bad):
    with pytest.raises((ProviderShapeError, ValueError)):
        parse_dispute(body(**bad))
