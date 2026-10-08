"""Kit 8's tool surface through the real ToolDispatcher (awx_agent.testing.KitToolHarness):
signed Cognito-shaped tokens, the per-kit reader, the propose path into a recorded Step
Functions execution. Every tool in TOOLS is covered for:

- the approval binding to the proposal hash;
- tenant isolation (the tenant comes from the verified token, never from the model);
- model-supplied values never choosing an amount, an account or a request_id;
- a re-proposal after the execution closed reporting EXISTING_PROPOSAL_*.
"""

from datetime import timedelta

import pytest
from awx_agent.testing import ADAPTER_FN, KitToolHarness
from awx_common import ApprovalDecision, Decision, approval_state
from awx_common.approvals import ApprovalBindingError
from awx_k8_settlement import TOOLS
from awx_k8_settlement import store as st
from awx_k8_settlement.operations import payout_request_id
from awx_spine_aws import codec
from awx_spine_aws.engine import MAX_RESENDS

CAT = "connected_account_transfers.create"
DAY = 86400.0
READS = sorted(t.name for t in TOOLS if t.name.startswith("read_"))
PROPOSES = sorted(t.name for t in TOOLS if t.name.startswith("propose_"))
# Values a model might try to slip in; every propose schema refuses them.
SMUGGLED = {
    "amount": "1.00",
    "destination": "acct_attacker",
    "source": "acct_attacker",
    "request_id": "00000000-0000-0000-0000-000000000000",
    "seller": "alice",
    "on_behalf_of": "acct_attacker",
}


class Surface:
    """A Market whose seller stores ARE the KitToolHarness's stores, seeded only through the
    shipped OPERATOR_COMMANDS. The runner and the tools read and write the same stores, so there
    is no copy step."""

    def __init__(self, h, platform_cash) -> None:
        self.m = h.Market(platform_cash, seed=False)
        self.harness = KitToolHarness.for_kit(
            "k8", port=self.m.port, tenants=dict(self.m.accounts), clock=self.m.now
        )
        self.m.stores = {s: self.harness.store(s) for s in h.SELLERS}
        h.seed_through_commands(self.m, self.harness.store)

    def call(self, tool, args, *, tenant=None, user="user-requester", groups=None):
        kw = {} if groups is None else {"groups": groups}
        token = self.harness.issuer.token(
            user, tenant_id=tenant, now=self.m.now().timestamp(), **kw
        )
        return self.harness.call(tool, args, token=token)


def valid_args(tool):
    return {
        "read_seller_balance": {"currency": "USD"},
        "read_seller_ledger": {},
        "read_carrier_event": {"carrier_event_id": "evt-bob-1"},
        "propose_settle_seller_cycle": {"cycle_id": "c2"},
        "propose_recalculate_seller_reserve": {"cycle_id": "c2", "carrier_event_id": "evt-bob-1"},
        "propose_reconcile_seller_money": {},
        "propose_recover_shortfall": {},
        "propose_resolve_in_flight_money": {
            "kind": "payout",
            "ref": "c1",
            "resolution": "not_landed",
            "evidence_ref": "awx-case:4711",
        },
    }[tool]


def stuck(h):
    """bob: c1's payout lost (in flight past the window) and 300 owed with a wallet to pay
    it, so every propose tool has something real to propose. The runner builds the in-flight
    and owed state on the harness's own stores; cycle data arrives through load_cycle."""
    surface = Surface(h, h.bob_c1_net())
    m, store_for = surface.m, surface.harness.store
    m.port.script(CAT, "lost", *(["lost"] * MAX_RESENDS))
    m.run(h.SETTLE, {"cycle_id": "c1"}, "bob")
    m.fund(h.usd(1100))  # refunds of 1100: 800 drawn from bob's reserve, 300 owed
    m.own_cash = h.usd(1100)
    h.load_cycle(m, store_for, "bob", "c0", 0, 1100, 0)
    assert m.run(h.SETTLE, {"cycle_id": "c0"}, "bob").state == "SUCCEEDED"  # 300 owed
    for s in h.SELLERS:  # a fresh cycle for everyone, to settle through the tool
        h.load_cycle(m, store_for, s, "c2", 1000, 0, 1000)
    m.offset_s = 7 * DAY
    m.run(h.RECONCILE, {}, "bob")
    m.set_wallet("bob", h.usd(1000))
    return surface


@pytest.fixture
def surface(h):
    return stuck(h)


def test_every_tool_is_covered():
    assert set(READS) | set(PROPOSES) == {t.name for t in TOOLS}
    assert len(PROPOSES) == 5
    assert len(READS) == 3


# --- reads ---------------------------------------------------------------------------------


@pytest.mark.parametrize("tool", READS)
def test_reads_answer_only_for_the_token_tenant_and_never_touch_the_adapter(h, surface, tool):
    out = surface.call(tool, valid_args(tool), tenant="bob")
    assert "untrusted_data" in out, out
    for call in surface.harness.reader.calls:
        assert call["FunctionName"] != ADAPTER_FN
        assert call["payload"]["tenant_id"] == "bob"
    assert surface.harness.sfn.calls == []


def test_a_read_cannot_be_pointed_at_another_seller(h, surface):
    bob = surface.call("read_seller_ledger", {}, tenant="bob")["untrusted_data"]
    cara = surface.call("read_seller_ledger", {}, tenant="cara")["untrusted_data"]
    assert bob["payout_pending"]["amount"] == "6960.00"
    assert cara["payout_pending"]["amount"] == "0.00"
    # bob's carrier evidence is not even indexed for cara; a forged copy under alice names bob
    assert "error" in surface.call(
        "read_carrier_event", valid_args("read_carrier_event"), tenant="cara"
    )
    assert "error" in surface.call(
        "read_carrier_event", valid_args("read_carrier_event"), tenant="alice"
    )
    # the wallet read is bound to the token tenant's account by the reader
    surface.m.set_wallet("cara", h.usd(42))
    out = surface.call("read_seller_balance", {"currency": "USD"}, tenant="cara")
    assert out["untrusted_data"]["available"]["amount"] == "42.00"
    obo = {c["payload"]["read"].get("on_behalf_of") for c in surface.harness.reader.calls}
    assert obo <= {surface.m.accounts["cara"], None}


@pytest.mark.parametrize("tool", READS + PROPOSES)
def test_no_tenant_claim_or_an_unknown_tenant_is_refused(h, surface, tool):
    assert surface.call(tool, valid_args(tool))["error"]["code"] == "NOT_AUTHORISED"
    assert "error" in surface.call(tool, valid_args(tool), tenant="mallory")
    assert surface.harness.sfn.calls == []


@pytest.mark.parametrize("tool", READS + PROPOSES)
@pytest.mark.parametrize("field", sorted(SMUGGLED))
def test_a_model_cannot_supply_an_amount_account_or_request_id(h, surface, tool, field):
    out = surface.call(tool, {**valid_args(tool), field: SMUGGLED[field]}, tenant="bob")
    assert out["error"]["code"] == "INVALID_INPUT", out
    assert surface.harness.sfn.calls == []


# --- proposes -------------------------------------------------------------------------------


def _execution(surface):
    (raw,) = surface.harness.sfn.executions.values()
    from awx_common import transport

    return transport.loads(raw)


@pytest.mark.parametrize("tool", PROPOSES)
def test_the_approval_binds_to_the_proposal_hash(h, surface, tool):
    out = surface.call(tool, valid_args(tool), tenant="bob")
    assert "proposal_id" in out, out
    proposal = codec.proposal_from(_execution(surface)["proposal"])
    proposal.verify_hash()
    if tool == "propose_reconcile_seller_money":  # justified AUTO floor: it sends nothing
        assert out["risk_tier"] == "AUTO"
        return
    assert out["risk_tier"] in ("SINGLE_APPROVER", "DUAL_APPROVER"), out
    assert proposal.intent_hash == out["intent_hash"]
    assert proposal.proposed_by == "user-requester"
    at = proposal.created_at + timedelta(minutes=1)
    good = ApprovalDecision(
        proposal.proposal_id, proposal.intent_hash, "approver-1", Decision.APPROVE, at
    )
    approval_state(proposal, [good], now=at)  # binds
    with pytest.raises(ApprovalBindingError):  # a decision for other amounts never binds
        approval_state(
            proposal,
            [ApprovalDecision(proposal.proposal_id, "f" * 64, "approver-1", Decision.APPROVE, at)],
            now=at,
        )
    with pytest.raises(ApprovalBindingError):  # nor can the proposer approve their own
        approval_state(
            proposal,
            [
                ApprovalDecision(
                    proposal.proposal_id,
                    proposal.intent_hash,
                    "user-requester",
                    Decision.APPROVE,
                    at,
                )
            ],
            now=at,
        )
    tampered = codec.proposal_from(_execution(surface)["proposal"])
    object.__setattr__(tampered, "computed", {**tampered.computed, "charge": h.usd(1)})
    with pytest.raises(ApprovalBindingError):
        tampered.verify_hash()


@pytest.mark.parametrize("tool", PROPOSES)
def test_the_seller_and_every_amount_come_from_the_token_tenant_and_the_kit(h, surface, tool):
    out = surface.call(tool, valid_args(tool), tenant="bob")
    execution = _execution(surface)
    assert execution["tenant_id"] == "bob"
    computed = execution["proposal"]["computed"]
    text = str(computed)
    assert "acct_attacker" not in text
    for other in ("alice", "cara"):
        assert surface.m.accounts[other] not in text
    if tool == "propose_settle_seller_cycle":  # the seller and payout come from the ledger
        assert computed["settlement"]["seller"] == "bob"
        assert out["status"] == "AWAITING_APPROVAL", out
    if tool == "propose_recover_shortfall":  # the amount is the ledger's receivable
        assert computed["charge"] == h.usd(300), out
    if tool == "propose_resolve_in_flight_money":  # amount and request_id from the store
        assert out["risk_tier"] == "DUAL_APPROVER", out
        assert computed["amount"] == h.usd(6960)
        book = st.book_id(surface.m.stores["bob"])
        assert computed["request_id"] == payout_request_id(book, "bob", "c1")


def test_the_same_arguments_under_another_tenant_are_another_proposal(h, surface):
    bob = surface.call("propose_settle_seller_cycle", {"cycle_id": "c2"}, tenant="bob")
    alice = surface.call("propose_settle_seller_cycle", {"cycle_id": "c2"}, tenant="alice")
    assert bob["summary"]["computed_by_code"]["settlement"]["seller"] == "bob"
    assert alice["summary"]["computed_by_code"]["settlement"]["seller"] == "alice"
    assert bob["proposal_id"] != alice["proposal_id"]
    by_tenant = {e["tenant_id"] for e in surface.harness.executions}
    assert by_tenant == {"bob", "alice"}


@pytest.mark.parametrize("tool", PROPOSES)
@pytest.mark.parametrize(
    ("status", "outcome", "expected"),
    [
        ("SUCCEEDED", None, "EXISTING_PROPOSAL_EXECUTION_SUCCEEDED"),
        ("FAILED", None, "EXISTING_PROPOSAL_EXECUTION_FAILED"),
        ("SUCCEEDED", "REJECTED", "EXISTING_PROPOSAL_REJECTED"),
    ],
)
def test_a_re_proposal_after_the_execution_closed_reports_the_existing_one(
    h, surface, tool, status, outcome, expected
):
    first = surface.call(tool, valid_args(tool), tenant="bob")
    (arn,) = surface.harness.sfn.executions
    surface.harness.sfn.status[arn] = status
    if outcome:
        surface.harness.outcomes[first["proposal_id"]] = outcome
    again = surface.call(tool, valid_args(tool), tenant="bob")
    assert again["proposal_id"] == first["proposal_id"]
    assert again["existing_proposal"] is True
    assert again["status"] == expected
    assert len(surface.harness.sfn.executions) == 1


# --- a counterparty scan with other sellers' money on the list ------------------------------


def test_a_counterparty_scan_under_bob_reveals_no_other_sellers_settled_money(h):
    """alice and cara are paid and SETTLED on the platform-wide transfer list. bob's lost c1
    payout is ruled not_landed, so the runner's counterparty scan (and the 24 h confirming
    scan) page through that list under bob's tenant. Nothing of alice's or cara's (account,
    object id, request_id, amount) may reach any output bob's token can see."""
    s = Surface(h, h.c1_net())
    m = s.m
    for other in ("alice", "cara"):
        assert m.run(h.SETTLE, {"cycle_id": "c1"}, other).state == "SUCCEEDED"
        (t,) = m.transfers_to(other)
        m.port.set_status(t["request_id"], "SETTLED")
    secrets = set()
    for other in ("alice", "cara"):
        secrets.add(m.accounts[other])
        for t in m.transfers_to(other):
            assert t["status"] == "SETTLED"
            secrets |= {t["id"], t["request_id"], str(t["amount"]), f"{t['amount']:.2f}"}
    m.port.script(CAT, "lost", *(["lost"] * MAX_RESENDS))
    outcomes = [m.run(h.SETTLE, {"cycle_id": "c1"}, "bob")]
    m.offset_s = 7 * DAY
    outcomes.append(m.run(h.RECONCILE, {}, "bob"))
    raw = {"kind": "payout", "ref": "c1", "resolution": "not_landed", "evidence_ref": "awx-case:1"}
    ruled = m.run(h.RESOLVE, raw, "bob")
    assert ruled.state == "SUCCEEDED", (ruled.detail, ruled.postcondition)  # the scan ran clean
    outcomes.append(ruled)
    m.advance(DAY + 60)
    outcomes.append(m.run(h.RECONCILE, {}, "bob"))  # the confirming read and scan
    scans = [c for c in m.port.reads if "connected_account_transfers" in c.path]
    assert scans, "the counterparty scan never listed the platform's transfers"
    listed = repr([m.fake.read(r) for r in scans])  # the scan really saw the others' money
    assert all(m.transfers_to(o)[0]["request_id"] in listed for o in ("alice", "cara"))
    seen = [
        repr((o.state, o.detail, o.postcondition, getattr(o.proposal, "reasons", None)))
        for o in outcomes
    ]
    for tool in READS + PROPOSES:
        seen.append(repr(s.call(tool, valid_args(tool), tenant="bob")))
    seen.append(repr([c["payload"] for c in s.harness.reader.calls]))
    seen.append(repr(s.harness.executions))
    leaks = sorted(x for x in secrets for text in seen if x in text)
    assert leaks == [], leaks
