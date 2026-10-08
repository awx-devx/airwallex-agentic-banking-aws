"""The kit 4 acceptance scenario, end to end the way a live run does it, marked `sandbox` (run
with `make sandbox-test`).

- fake: FakeAirwallex through local_runner.
- live: the real Airwallex sandbox. Skipped: payment_intents/{id}/confirm returns 400
  "This account is not enabled for native API access" until Airwallex enables online payments
  (payment acceptance) on the sandbox account, and a dispute can only be staged on a paid intent.

Rules for a live run: a fresh book id per run (M2), poc-i4- labels on every sandbox
object the setup creates, `flock /tmp/awx-sandbox.lock` held for the whole run, and at the end
no dispute left waiting on a response this kit sent.
"""

from __future__ import annotations

import pytest
from awx_common import Money, RiskTier
from awx_k4_dispute import SPECS
from awx_k4_dispute.config import DEFAULT_CONFIG
from awx_k4_dispute.simulation import DEMO_CASES
from k4_harness import World, accept_input, approve, challenge_input, run, sandbox_lock

LIVE_SKIP = (
    "Airwallex has not enabled online payments on the sandbox account: "
    "POST /api/v1/pa/payment_intents/{id}/confirm returns 400 validation_error 'This account "
    "is not enabled for native API access', and a dispute can only be staged on a paid intent"
)


@pytest.mark.sandbox
@pytest.mark.parametrize(
    "target", ["fake", pytest.param("live", marks=pytest.mark.skip(LIVE_SKIP))]
)
def test_kit4_acceptance_scenario(target):
    assert target == "fake"  # the live variant is skipped (see LIVE_SKIP)
    with sandbox_lock():
        w = World(label_prefix="poc-i4")
        strong, small, credit = DEMO_CASES
        assert w.book.startswith("bk_")
        sid = w.add_dispute("poc_i4_strong", amount="900.00", reason={"code": "10.4"}, stage="RFI")
        mid = w.add_dispute(
            "poc_i4_small", amount="15.00", reason={"code": "13.1"}, stage="CHARGEBACK"
        )
        cid = w.add_dispute("poc_i4_credit", amount="180.00", reason={"code": "13.6"})
        assert all(
            pi["merchant_order_id"].startswith("poc-i4-" + w.book)
            for pi in w.fake.objects("payment_intents")
        )
        # strong: challenge with JPG/PDF evidence, approved
        out = run(SPECS["challenge_dispute"], challenge_input(sid), port=w, decide=approve("a-1"))
        assert out.state == "SUCCEEDED", out.detail
        # small: cannot be accepted without the bound approval (no decision, self-approval)
        unbound = run(SPECS["accept_dispute"], accept_input(mid, "15.00", "LOW_VALUE"), port=w)
        assert unbound.state == "EXPIRED"
        self_ok = run(
            SPECS["accept_dispute"],
            accept_input(mid, "15.00", "LOW_VALUE"),
            port=w,
            decide=approve("requester-1"),
        )
        assert self_ok.state == "REJECTED"
        assert w.writes("disputes.accept") == []
        # small: accept, approved by someone else
        acc = run(
            SPECS["accept_dispute"],
            accept_input(mid, "15.00", "LOW_VALUE"),
            port=w,
            decide=approve("a-1"),
        )
        assert acc.state == "SUCCEEDED", acc.detail
        # credit: refer to a person
        ref = run(
            SPECS["refer_dispute"], {"dispute_id": cid, "summary": "No support reply."}, port=w
        )
        assert ref.decision.risk_tier is RiskTier.AUTO
        # the issuer rejects: escalate, no second challenge, resolve LOST, close typed
        w.escalate(sid)
        again = run(SPECS["challenge_dispute"], challenge_input(sid), port=w, decide=approve("a-1"))
        assert again.state == "BLOCKED"
        w.resolve(sid, "LOST")
        closed = run(
            SPECS["close_dispute"],
            {"dispute_id": sid, "close_reason": "LOST_ISSUER_REJECTED_EVIDENCE"},
            port=w,
        )
        assert closed.state == "SUCCEEDED", closed.detail
        # the configured (modelled) fee and the provider outcome are recorded at close
        entries = {e["kind"]: e for e in closed.decision.computed["ledger_entries"]}
        assert entries["DISPUTE_FEE"]["source"] == "MODELLED"
        assert entries["DISPUTE_FEE"]["amount"] == DEFAULT_CONFIG.terms["AUD"].dispute_fee
        assert entries["DISPUTED_AMOUNT_LOST"]["source"] == "PROVIDER"
        assert entries["DISPUTED_AMOUNT_LOST"]["amount"] == Money.of("900.00", "AUD")
        assert w.case(sid)["outcome"]["close_reason"] == "LOST_ISSUER_REJECTED_EVIDENCE"
        assert len(w.writes("disputes.challenge")) == 1
        assert len(w.writes("disputes.accept")) == 1
        # end state: nothing this kit sent is left in flight
        assert {w.disputes[sid]["status"], w.disputes[mid]["status"]} == {"LOST", "ACCEPTED"}
        assert "lock" not in w.case(sid)
        assert "lock" not in w.case(mid)
    assert (strong.reason_code, small.reason_code, credit.reason_code) == ("10.4", "13.1", "13.6")
