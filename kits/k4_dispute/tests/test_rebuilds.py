"""Runner rule 0: every build runs twice for one proposal and each action still sends once."""

from __future__ import annotations

from dataclasses import replace

from awx_k4_dispute import READS, SPECS
from k4_harness import accept_input, approve, challenge_input, make_ctx, past_clear_bound, run

ACCEPT = SPECS["accept_dispute"]
CHALLENGE = SPECS["challenge_dispute"]
REFER = SPECS["refer_dispute"]
CLOSE = SPECS["close_dispute"]
CLEAR = SPECS["clear_dispute_response"]


def twice(spec):
    """Every build runs twice for the same proposal; the first result is thrown away (rule 0)."""

    def wrap(build):
        def built(ctx):
            build(ctx)
            return build(ctx)

        return built

    return replace(spec, steps=tuple(replace(st, build=wrap(st.build)) for st in spec.steps))


def case_refs(port, dispute_id):
    return READS["read_dispute"]({"dispute_id": dispute_id}, make_ctx(port, ACCEPT))["case"]


# ---- rule 0: every build, twice ------------------------------------------------------------


def test_rule0_challenge_builds_twice_send_once(port, strong_case):
    out = run(twice(CHALLENGE), challenge_input(strong_case), port=port, decide=approve("a-1"))
    assert out.state == "SUCCEEDED", out.detail
    assert out.postcondition.ok
    assert len(port.writes("disputes.challenge")) == 1
    assert len(port.writes("files.upload")) == 2
    case = port.case(strong_case)
    assert "lock" not in case
    assert len(case["attempts"]) == 1


def test_rule0_accept_builds_twice_send_once(port, small_case):
    raw = accept_input(small_case, "15.00", "LOW_VALUE")
    out = run(twice(ACCEPT), raw, port=port, decide=approve("a-1"))
    assert out.state == "SUCCEEDED", out.detail
    assert out.postcondition.ok
    assert len(port.writes("disputes.accept")) == 1
    assert "lock" not in port.case(small_case)
    assert len(port.case(small_case)["attempts"]) == 1


def test_rule0_refer_close_and_clear_build_twice(port, credit_case, strong_case):
    raw = {"dispute_id": credit_case, "summary": "two emails"}
    assert run(twice(REFER), raw, port=port).postcondition.ok
    port.resolve(credit_case, "EXPIRED")
    close = {"dispute_id": credit_case, "close_reason": "EXPIRED_NO_RESPONSE"}
    assert run(twice(CLOSE), close, port=port).postcondition.ok
    assert case_refs(port, credit_case)["referrals"] == 1
    port.timeout("disputes.challenge", landed=False)
    run(CHALLENGE, challenge_input(strong_case), port=port, decide=approve("a-1"))
    past_clear_bound(port)
    ref = case_refs(port, strong_case)["attempt_refs"][0]["request_ref"]
    clear = {"dispute_id": strong_case, "request_ref": ref, "note": "checked the dashboard"}
    out = run(twice(CLEAR), clear, port=port, decide=approve("a-1"))
    assert out.state == "SUCCEEDED", out.detail
    assert "lock" not in port.case(strong_case)
    assert port.writes()
    assert all(w.name != "disputes.accept" for w in port.writes())
