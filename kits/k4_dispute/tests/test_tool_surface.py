"""Every tool through awx_agent.testing.KitToolHarness (the real ToolDispatcher, reader and
approval path), and the kit's module-level imports."""

from __future__ import annotations

import pytest
from awx_agent.testing import ADAPTER_FN, KitToolHarness
from awx_common import RiskTier
from awx_k4_dispute import SPECS, TOOLS
from awx_k4_dispute.config import DEFAULT_CONFIG
from awx_k4_dispute.evidence import EvidenceKind, ingest_evidence
from awx_k4_dispute.provider import accept_request_id
from k4_harness import (
    FIXTURES,
    STRONG_FACTS,
    World,
    accept_input,
    challenge_input,
    evidence_text,
)

CHALLENGE = SPECS["challenge_dispute"]
ACCEPT = SPECS["accept_dispute"]
CLEAR = SPECS["clear_dispute_response"]

# ---- every tool through the real ToolDispatcher -------------------------------------------


@pytest.fixture
def tools():
    w = World(auto_ingest=False)
    w.add_dispute("dsp_t", amount="900.00", reason={"code": "10.4"})
    w.add_dispute("dsp_s", amount="15.00", reason={"code": "13.1"}, stage="CHARGEBACK")
    from datetime import UTC, datetime

    fixed = datetime.now(UTC).replace(microsecond=0)  # the harness signs tokens at this clock
    h = KitToolHarness.for_kit("k4", port=w.fake, clock=lambda: fixed)
    for rel in ("strong_fraud/ev_order_history.pdf", "strong_fraud/ev_signature.jpg"):
        path = FIXTURES / rel
        kind = EvidenceKind.ORDER_HISTORY if "order" in rel else EvidenceKind.DELIVERY_PROOF
        ingest_evidence(
            h.store(),
            "dsp_t",
            path.stem,
            path.name,
            path.read_bytes(),
            kind,
            DEFAULT_CONFIG,
            text=evidence_text(rel),
        )
    return h, w


TOOL_ARGS = {
    "read_dispute_queue": {},
    "read_dispute": {"dispute_id": "dsp_t"},
    "read_dispute_economics": {
        "dispute_id": "dsp_t",
        "evidence_ids": ["ev_order_history", "ev_signature"],
        "facts": STRONG_FACTS,
    },
    "read_refunds": {"dispute_id": "dsp_t"},
    "propose_accept_dispute": accept_input("dsp_s", "15.00", "LOW_VALUE"),
    "propose_challenge_dispute": challenge_input("dsp_t"),
    "propose_refer_dispute": {"dispute_id": "dsp_t", "summary": "s"},
    "propose_close_dispute": {"dispute_id": "dsp_t", "close_reason": "WON_EVIDENCE_ACCEPTED"},
    "propose_clear_dispute_response": {
        "dispute_id": "dsp_t",
        "request_ref": "0" * 8 + "-0000-0000-0000-" + "0" * 12,
        "note": "n",
    },
}


def test_every_tool_has_an_argument_set_here():
    assert set(TOOL_ARGS) == {t.name for t in TOOLS}


@pytest.mark.parametrize("name", sorted(t.name for t in TOOLS if t.name.startswith("read_")))
def test_read_tools_go_through_the_kit_reader_as_the_platform(tools, name):
    h, _ = tools
    out = h.call(name, TOOL_ARGS[name])
    assert "untrusted_data" in out, out
    assert h.reader.calls
    assert {c["FunctionName"] for c in h.reader.calls} != {ADAPTER_FN}
    assert all(c["payload"]["tenant_id"] == "platform" for c in h.reader.calls)
    assert h.sfn.calls == []


@pytest.mark.parametrize("name", sorted(t.name for t in TOOLS if t.name.startswith("propose_")))
def test_propose_tools_bind_the_approval_to_the_proposal_hash(tools, name):
    h, _ = tools
    out = h.call(name, TOOL_ARGS[name])
    assert "proposal_id" in out, out
    (execution,) = h.executions
    p = execution["proposal"]
    assert p["intent_hash"] == out["intent_hash"]
    assert execution["origin"] == "AGENT"
    floor = SPECS[name.removeprefix("propose_")].agent_min_tier
    if floor is not RiskTier.AUTO:
        assert out["risk_tier"] != "AUTO"
    from awx_spine_aws import codec

    codec.proposal_from(p).verify_hash()  # tampering would fail here


@pytest.mark.parametrize("name", sorted(t.name for t in TOOLS if t.name.startswith("propose_")))
def test_a_reproposal_after_the_execution_closed_reports_existing(tools, name):
    h, _ = tools
    first = h.call(name, TOOL_ARGS[name])
    (arn,) = h.sfn.executions
    h.sfn.status[arn] = "SUCCEEDED"
    again = h.call(name, TOOL_ARGS[name])
    assert again["proposal_id"] == first["proposal_id"]
    assert again["status"] == "EXISTING_PROPOSAL_EXECUTION_SUCCEEDED"
    assert len(h.sfn.executions) == 1


@pytest.mark.parametrize(
    "extra",
    [
        {"request_id": "x"},
        {"account_id": "acct_x"},
        {"on_behalf_of": "acct_x"},
        {"amount": "1.00"},
        {"tenant_id": "other"},
    ],
)
def test_model_supplied_server_values_are_refused(tools, extra):
    h, _ = tools
    for name in ("propose_accept_dispute", "propose_challenge_dispute"):
        out = h.call(name, {**TOOL_ARGS[name], **extra})
        assert out["error"]["code"] == "INVALID_INPUT", out
    assert h.sfn.calls == []


def test_amount_and_request_id_come_from_code_not_the_model(tools):
    h, _ = tools
    out = h.call("propose_accept_dispute", accept_input("dsp_s", "14.00", "LOW_VALUE"))
    assert out["risk_tier"] == "BLOCKED"  # the model's amount differs from Airwallex's
    out = h.call("propose_accept_dispute", TOOL_ARGS["propose_accept_dispute"])
    computed = out["summary"]["computed_by_code"]
    assert computed["request_id"] == accept_request_id("dsp_s", "CHARGEBACK")
    assert computed["disputed_amount"] == {"amount": "15.00", "currency": "AUD"}


def test_tenant_isolation_kit4_runs_as_the_platform_whatever_the_claim(tools):
    """Kits 1-4 move the platform's own money: a tenant claim is ignored, the kit store and
    every read are the platform's, and no read carries x-on-behalf-of."""
    h, _ = tools
    h.store("tenant-b").put(
        "evidence/dsp_t/ev_planted", {"dispute_id": "dsp_t"}, expected_version=None
    )
    raw = dict(challenge_input("dsp_t"), evidence_ids=["ev_order_history", "ev_planted"])
    out = h.call("propose_challenge_dispute", raw, tenant_id="tenant-b")
    assert out["risk_tier"] == "BLOCKED"  # tenant-b's planted entry is invisible
    assert any("ev_planted" in r for r in out["reasons"])
    assert all(c["payload"]["tenant_id"] == "platform" for c in h.reader.calls)
    assert all(c["payload"]["read"].get("on_behalf_of") is None for c in h.reader.calls)


def test_kit_source_imports_only_awx_common_and_itself_at_module_level():
    """Kit source imports only awx_common and itself at module level. The upload handler's AWS
    adapters (boto3, the spine's DynamoDB KitStore) are imported inside functions, at invocation
    only."""
    import ast
    from pathlib import Path

    src = Path(__file__).parents[1] / "src" / "awx_k4_dispute"
    for path in src.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in tree.body:
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            for n in names:
                root = n.split(".")[0]
                assert root in {"awx_common", "awx_k4_dispute", "__future__"} or root in (
                    __import__("sys").stdlib_module_names
                ), f"{path.name} imports {n} at module level"
