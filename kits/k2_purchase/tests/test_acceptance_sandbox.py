"""Kit 2 required acceptance scenario (the limit-boundary test on a pre-funded wallet), marked
`sandbox`. `make check` skips it; run it with `make sandbox-test` or `pytest -m sandbox`.

By default it runs the real runner against FakeAirwallex. Set AWX_K2_LIVE=1 to run the same
steps against the Airwallex sandbox through awx_spine.AirwallexClient, holding
`flock /tmp/awx-sandbox.lock` for the whole run so no other kit races on the shared platform
balance.

The scenario: pre-funded wallet, a dedicated limit-test card issued with approval, then the
five probes: exactly the limit clears, limit + 0.01 is LIMIT_EXCEEDED, a disallowed currency and
merchant category fail with their own reasons, the cumulative limit then blocks, and
INSUFFICIENT_FUNDS anywhere would be a harness failure. Then the card is closed, also when an
assertion fails (try/finally: close, or freeze if close does not succeed). Objects carry
`poc-i2-<run>` (in the purchase ref, so in the card nick name and cardholder email); a fresh book
id per run (M2) keeps request_ids new. At the end no authorisation is PENDING (single_phase) and
kit 2 created no transfer, so nothing can sit at SENT.
"""

from __future__ import annotations

import fcntl
import os
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from awx_common import Money
from awx_common.ops import ReadRequest
from awx_spine.fake import FakeAirwallex
from k2env import K2Env, guide_terms

pytestmark = pytest.mark.sandbox

LIVE = os.environ.get("AWX_K2_LIVE") == "1"
LOCK = "/tmp/awx-sandbox.lock"  # noqa: S108  shared by every kit's live run
PRICE = "2.00"  # the at-limit debit: the only money the scenario moves


@contextmanager
def _sandbox_lock():
    if not LIVE:
        yield
        return
    with open(LOCK, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


class _LiveEnv(K2Env):
    """K2Env over the real sandbox: wall clock, real sleep, the spine's client."""

    def __init__(self) -> None:
        from awx_spine.client import AirwallexClient
        from awx_spine.credentials import EnvFileCredentials

        super().__init__(fake=FakeAirwallex())  # placeholder; replaced below
        self.fake = _LivePort(AirwallexClient(EnvFileCredentials()))

    def now(self) -> datetime:
        return datetime.now(UTC)


class _LivePort:
    """The few fake helpers K2Env uses, answered from the live API."""

    def __init__(self, client) -> None:
        self.client = client
        self.requests: list[tuple[str, str, str | None]] = []

    def execute(self, op):
        self.requests.append((op.method, op.path, op.on_behalf_of))
        return self.client.execute(op)

    def read(self, req):
        return self.client.read(req)

    def advance(self, seconds: float) -> None:
        time.sleep(seconds)

    def clock(self) -> float:
        return time.monotonic()

    def objects(self, kind: str):
        raise NotImplementedError("live runs assert through reads")


def _card_of(outcome) -> str | None:
    for result in outcome.results.get("card") or ():
        if result.provider_id:
            return result.provider_id
    return None


def _find_our_card(env: K2Env, ref: str) -> str | None:
    """The card this run's book created, found by its cardholder's book-scoped email."""
    from awx_k2_purchase.ops import cardholder_email

    book = env.kit_store.get("book")
    if book is None:
        return None
    email = cardholder_email(book.value["book_id"], ref)
    holders = env.fake.read(ReadRequest("/api/v1/issuing/cardholders", {"email": email}))
    for holder in holders.get("items", []):
        cards = env.fake.read(
            ReadRequest("/api/v1/issuing/cards", {"cardholder_id": holder["cardholder_id"]})
        )
        for card in cards.get("items", []):
            return card["card_id"]
    return None


def _close_or_freeze(env: K2Env, card_id: str) -> None:
    try:
        if env.run("close_card", {"card_id": card_id}, "approver").state == "SUCCEEDED":
            return
    except Exception:  # noqa: S110  fall through to the freeze
        pass
    env.run("freeze_card", {"card_id": card_id})


def _env() -> K2Env:
    if LIVE:
        return _LiveEnv()
    return K2Env(balances={"USD": "30000.00"})


def test_a_failed_acceptance_run_still_closes_its_card(monkeypatch):
    """On the fake: force the limit test to fail; the card still ends CLOSED."""
    import k2env

    env = K2Env(balances={"USD": "30000.00"})
    monkeypatch.setattr(sys.modules[__name__], "_env", lambda: env)
    real = k2env.K2Env.run

    def failing(self, action, raw, *a, **kw):
        out = real(self, action, raw, *a, **kw)
        if action == "run_limit_test":
            raise AssertionError("forced failure after the probes")
        return out

    monkeypatch.setattr(k2env.K2Env, "run", failing)
    with pytest.raises(AssertionError, match="forced failure"):
        test_kit2_acceptance_limit_boundary_on_a_prefunded_wallet()
    (card,) = env.fake.objects("cards")
    assert card["card_status"] == "CLOSED"


def test_a_failed_issue_step_still_closes_the_card(monkeypatch):
    """The issue step fails after the card was created (its postcondition fails); the card is
    still closed."""
    import k2env

    env = K2Env(balances={"USD": "30000.00"})
    monkeypatch.setattr(sys.modules[__name__], "_env", lambda: env)
    real = k2env.K2Env.run

    def failing(self, action, raw, *a, **kw):
        out = real(self, action, raw, *a, **kw)
        if action == "issue_card":
            return replace(out, state="POSTCONDITION_FAILED")
        return out

    monkeypatch.setattr(k2env.K2Env, "run", failing)
    with pytest.raises(AssertionError):
        test_kit2_acceptance_limit_boundary_on_a_prefunded_wallet()
    (card,) = env.fake.objects("cards")
    assert card["card_status"] == "CLOSED"


def test_a_close_that_raises_falls_back_to_freeze(monkeypatch):
    import k2env

    env = K2Env(balances={"USD": "30000.00"})
    monkeypatch.setattr(sys.modules[__name__], "_env", lambda: env)
    real = k2env.K2Env.run

    def failing(self, action, raw, *a, **kw):
        if action in ("run_limit_test", "close_card"):
            raise RuntimeError(f"forced {action}")
        return real(self, action, raw, *a, **kw)

    monkeypatch.setattr(k2env.K2Env, "run", failing)
    with pytest.raises(RuntimeError, match="forced run_limit_test"):
        test_kit2_acceptance_limit_boundary_on_a_prefunded_wallet()
    (card,) = env.fake.objects("cards")
    assert card["card_status"] == "INACTIVE"


def test_kit2_acceptance_limit_boundary_on_a_prefunded_wallet():
    run = uuid.uuid4().hex[:7]
    ref = f"limit-test-poc-i2-{run}"
    with _sandbox_lock():
        env = _env()
        terms = guide_terms(
            purchase_ref=ref, monthly_price=PRICE, annual_discount_percent="0", forecast_flows=[]
        )
        card_id = None
        closed = False
        try:
            issued = env.run("issue_card", terms, "approver")
            card_id = _card_of(issued) or _find_our_card(env, ref)
            assert issued.state == "SUCCEEDED", (issued.detail, issued.postcondition)
            out = env.run("run_limit_test", {"card_id": card_id}, "approver")
            assert out.state == "SUCCEEDED", (out.detail, out.postcondition)
            assert out.proposal.computed["debit"] == Money.of(PRICE, "USD").to_dict()
            replies = {k: v[0].body for k, v in out.results.items()}
            got = {k: (r["process_result"], r.get("failure_reason")) for k, r in replies.items()}
            assert got == {
                "probe_currency": ("DECLINED", "CURRENCY_NOT_ALLOWED"),
                "probe_mcc": ("DECLINED", "MERCHANT_CATEGORY_NOT_ALLOWED"),
                "probe_over_limit": ("DECLINED", "LIMIT_EXCEEDED"),
                "probe_at_limit": ("APPROVED", None),
                "probe_cumulative": ("DECLINED", "LIMIT_EXCEEDED"),
            }
            assert "INSUFFICIENT_FUNDS" not in out.postcondition.detail
            closed_out = env.run("close_card", {"card_id": card_id}, "approver")
            assert closed_out.state == "SUCCEEDED", closed_out.detail
            closed = True
        finally:
            if not closed:  # never leave a live card with authority
                card_id = card_id or _find_our_card(env, ref)
                if card_id is not None:
                    _close_or_freeze(env, card_id)
        print(f"poc-i2 run {run}: card {card_id}, nick poc-k2-{ref}"[:120])

        rows = env.fake.read(ReadRequest("/api/v1/issuing/transactions", {"card_id": card_id})).get(
            "items", []
        )
        assert not [r for r in rows if r.get("status") == "PENDING"]
        assert not [p for m, p, _ in env.fake.requests if m == "POST" and "/transfers" in p]
