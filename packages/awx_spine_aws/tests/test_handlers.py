"""Lambda handlers against moto tables, with the toy port behind awx_spine's real executor.

The state machine's own sequence is checked in test_asl_equivalence.py, which interprets the
synthesised ASL; these tests pin each handler's contract.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from awx_common import Origin, RiskTier
from awx_common.approvals import ApprovalBindingError, ApprovalDecision, Decision
from awx_common.errors import TenantScopeViolation
from awx_common.ops import ReadRequest
from awx_common.transport import decode, encode
from awx_spine_aws import engine
from awx_spine_aws.handlers import adapter, approval, pre_token, reader
from awx_spine_aws.repos import DecisionRepo, ProposalRepo

import toy
from aws_harness import execution, sfn_json, start, step, use_spec


def _approve(aws, run, approver="approver-sub"):
    p = ProposalRepo("proposals", client=aws.db).get(run["proposal"]["proposal_id"]).proposal
    DecisionRepo("decisions", client=aws.db).put(
        ApprovalDecision(
            p.proposal_id,
            p.intent_hash,
            approver,
            Decision.APPROVE,
            p.created_at + timedelta(seconds=1),
        )
    )


def adapter_call(run, mode=None, **extra):
    pending = run["step"]["pending"]
    event = {
        "mode": mode or pending["action"],
        "operation": pending["op"],
        "proposal_id": run["proposal"]["proposal_id"],
        **extra,
    }
    return sfn_json(adapter.handler(sfn_json(event), None))


# --- reader --------------------------------------------------------------------------------


def _read(event):
    return decode(reader.handler(sfn_json(encode(event)), None))


BAL = "/api/v1/balances/current"
BEN = "/api/v1/beneficiaries/ben_1"  # on read_allowlist only (toy.TENANT_ONLY)
TFR = "/api/v1/transfers/tfr_9"


def test_reader_tenant_read_is_bound_to_the_server_resolved_tenant(aws):
    req = ReadRequest(BAL, on_behalf_of="acct_A")
    out = _read({"kit": "k3", "read": req, "tenant_id": "tenant-a"})
    assert out["body"]["items"][0]["available_amount"] == toy.DEC("1000.00")  # exact Decimal
    assert aws.port.reads[-1].on_behalf_of == "acct_A"


def test_reader_derives_a_platform_read_from_a_headerless_request(aws):
    """Scope is derived; a "mode" field cannot widen it."""
    aws.port.created["r"] = {"id": "tfr_9", "status": "PAID", "_reads": 0}
    _read({"kit": "k3", "read": ReadRequest(TFR), "tenant_id": "tenant-a"})
    assert aws.port.reads[-1].on_behalf_of is None
    with pytest.raises(TenantScopeViolation, match="platform read"):  # not platform-listed
        _read({"kit": "k3", "read": ReadRequest(BEN), "tenant_id": "tenant-a", "mode": "tenant"})


def test_a_tenant_listed_path_is_readable_inside_the_tenant(aws):
    _read({"kit": "k3", "read": ReadRequest(BEN, on_behalf_of="acct_A"), "tenant_id": "tenant-a"})
    assert aws.port.reads[-1].on_behalf_of == "acct_A"


def test_a_platform_only_kit_reads_its_own_account_under_read_allowlist(aws, monkeypatch):
    """Kits 1 to 4 declare no platform list; their platform-tenant reads use read_allowlist."""
    allow = {"k3": {"tenant": [r"^/api/v1/balances/current$"], "platform": []}}
    monkeypatch.setenv("AWX_READ_ALLOWLISTS", json.dumps(allow))
    _read({"kit": "k3", "read": ReadRequest(BAL), "tenant_id": None})


def test_the_platform_tenant_reads_its_own_account(aws):
    aws.port.created["r"] = {"id": "tfr_9", "status": "PAID", "_reads": 0}
    _read({"kit": "k3", "read": ReadRequest(BAL), "tenant_id": None})
    _read({"kit": "k3", "read": ReadRequest(TFR), "tenant_id": None})
    assert {r.on_behalf_of for r in aws.port.reads} == {None}


def test_a_reader_without_a_fixed_kit_refuses_everything(aws, monkeypatch):
    """There is no shared reader: the kit never comes from the payload alone."""
    monkeypatch.delenv("AWX_READER_KIT")
    with pytest.raises(TenantScopeViolation, match="no shared reader"):
        _read({"kit": "k3", "read": ReadRequest(BAL), "tenant_id": None})
    assert aws.port.reads == []


def test_a_per_kit_reader_ignores_and_refuses_other_kits(aws, monkeypatch):
    _read({"read": ReadRequest(BAL), "tenant_id": None})  # kit taken from configuration
    with pytest.raises(TenantScopeViolation, match="serves k3"):
        _read({"kit": "k4", "read": ReadRequest(BAL), "tenant_id": None})


@pytest.mark.parametrize(
    "event",
    [
        {"kit": "k3", "read": ReadRequest("/api/v1/beneficiaries"), "tenant_id": None},
        {"kit": "k4", "read": ReadRequest(BAL), "tenant_id": None},
        {"kit": "k99", "read": ReadRequest(BAL), "tenant_id": None},
        {"kit": "k3", "read": ReadRequest(BAL), "tenant_id": "nobody"},
        {"kit": "k3", "read": ReadRequest(BAL, on_behalf_of="acct_B"), "tenant_id": "tenant-a"},
        {"kit": "k3", "read": ReadRequest(BEN), "tenant_id": "tenant-a", "mode": "tenant"},
        {"kit": "k3", "read": ReadRequest(BEN), "tenant_id": None},
        {"kit": "k3", "read": ReadRequest(TFR, on_behalf_of="acct_A"), "tenant_id": None},
        {
            "kit": "k3",
            "read": ReadRequest("/api/v1/transfers/../balances/current"),
            "tenant_id": None,
        },
    ],
    ids=[
        "path-not-allowlisted",
        "kit-has-no-allowlist",
        "unknown-kit",
        "unknown-tenant",
        "names-another-account",
        "headerless-not-platform-listed",
        "platform-tenant-held-to-platform-list",
        "platform-tenant-with-header",
        "non-canonical-path",
    ],
)
def test_reader_refuses(aws, event):
    with pytest.raises(TenantScopeViolation):
        reader.handler(sfn_json(encode(event)), None)
    assert aws.port.reads == []


# --- kit step ------------------------------------------------------------------------------


def pid(run):
    return run["proposal"]["proposal_id"]


def test_start_stores_the_proposal_with_the_resolved_tenant(aws, testkit):
    run = start("tenant-a")
    stored = ProposalRepo("proposals", client=aws.db).get(pid(run))
    assert stored.tenant_id == "tenant-a"
    assert run["tenant"]["connected_account_id"] == "acct_A"
    assert run["proposal"]["risk_tier"] == "SINGLE_APPROVER"
    assert run["proposal"]["origin"] == "SCHEDULE"


def test_start_redelivered_returns_the_same_proposal(aws, testkit):
    name, data = execution()
    first = step(engine.Phase.START, execution=data, execution_name=name)
    again = step(engine.Phase.START, execution=data, execution_name=name)
    assert again["proposal"] == first["proposal"]


def test_start_refuses_an_execution_not_named_after_its_proposal_and_audits(aws, testkit):
    _, data = execution()
    with pytest.raises(engine.WorkflowDefinitionError, match="execution name"):
        step(engine.Phase.START, execution=data, execution_name="other")
    [item] = aws.db.scan(TableName="audit")["Items"]
    assert item["proposal_id"]["S"] == "other"
    assert item["state"]["S"] == "REFUSED"


def test_start_refuses_a_tampered_proposal(aws, testkit):
    name, data = execution()
    data["proposal"]["computed"] = {"total": {"$money": ["1.00", "AUD"]}}
    with pytest.raises(ApprovalBindingError):
        step(engine.Phase.START, execution=data, execution_name=name)


def test_start_applies_the_agent_floor(aws, testkit):
    use_spec(testkit, toy.toy_spec(RiskTier.AUTO))
    run = start(origin=Origin.AGENT)
    assert run["proposal"]["risk_tier"] == "SINGLE_APPROVER"


def test_kit_exceptions_fail_the_run_closed_inside_the_lambda(aws, testkit):
    def broken(ctx):
        raise ValueError("bad build")

    use_spec(testkit, toy.toy_spec(RiskTier.AUTO, fee_build=broken))
    run = step(engine.Phase.RECHECK, start())
    run = step(engine.Phase.BUILD, run, step="fee")
    assert run["state"] == "FAILED"
    assert "ValueError: bad build" in run["detail"]
    assert run["postcondition_due"] is True  # rule 2: AfterStop runs the postcondition


def test_error_phase_reads_the_lambda_error_envelope(aws, testkit):
    run = start()
    cause = json.dumps({"errorType": "Boom", "errorMessage": "it broke"})
    out = step(engine.Phase.ERROR, run, error={"Error": "Boom", "Cause": cause})
    assert out["state"] == "FAILED"
    assert out["detail"] == "Boom: it broke"


def test_audit_is_written_once_even_when_the_state_is_retried(aws, testkit):
    run = start()
    step(engine.Phase.AUDIT, run)
    step(engine.Phase.AUDIT, run)  # a retried Audit state
    assert len(aws.db.scan(TableName="audit")["Items"]) == 1


def _executing(aws, testkit, *, tenant_id=None, spec=None):
    use_spec(testkit, spec or toy.toy_spec(RiskTier.AUTO))
    run = step(engine.Phase.RECHECK, start(tenant_id))
    return step(engine.Phase.BUILD, run, step="fee")


def test_lambda_platform_store_is_the_platform_ledger_partition(aws, testkit):
    from awx_common.tenancy import TenantContext
    from awx_spine_aws.handlers import kit_step

    lenv = kit_step.LambdaEnv()
    store = lenv.store("k3", TenantContext("platform"))
    store.put("pool", {"n": 1}, expected_version=None)
    root = kit_step.root_spec()
    ctx = engine.policy_context(root, lenv, TenantContext("tenant-a", "acct_A"))
    assert ctx.platform_store.get("pool").value == {"n": 1}
    items = aws.db.scan(TableName="ledger")["Items"]
    assert [(i["pk"]["S"], i["sk"]["S"]) for i in items] == [("k3#platform", "pool")]


# --- adapter -------------------------------------------------------------------------------


def test_adapter_refuses_an_unapproved_proposal(aws, testkit):
    run = step(engine.Phase.BUILD, start(), step="fee")  # SINGLE, nobody approved
    with pytest.raises(adapter.NotAuthorised, match="PENDING"):
        adapter_call(run)
    assert aws.port.sends == []


def test_adapter_refuses_an_operation_for_another_proposal(aws, testkit):
    run = _executing(aws, testkit)
    run["step"]["pending"]["op"]["intent_hash"] = "0" * 64
    with pytest.raises(adapter.NotAuthorised, match="not bound"):
        adapter_call(run)
    assert aws.port.sends == []


@pytest.mark.parametrize(
    "change",
    [{"on_behalf_of": "acct_B"}, {"on_behalf_of": None}],
    ids=["other-account", "unbound"],
)
def test_adapter_checks_tenant_scope_as_defence_in_depth(aws, testkit, change):
    run = _executing(aws, testkit, tenant_id="tenant-a")
    run["step"]["pending"]["op"].update(change)
    with pytest.raises(TenantScopeViolation):
        adapter_call(run)
    assert aws.port.sends == []


def test_adapter_sends_the_body_exactly_as_built_and_scopes_evidence(aws, testkit):
    run = _executing(aws, testkit, tenant_id="tenant-a")
    op = run["step"]["pending"]["op"]
    adapter_call(run)
    sent = aws.port.sends[-1]
    assert dict(sent.body) == decode(op)["body"]  # sent exactly as built: nothing added
    assert aws.executors == [("k3", "tenant-a")]


def test_adapter_returns_an_unknown_outcome_instead_of_raising(aws, testkit):
    aws.port.unknown_times["beneficiaries.create"] = 1
    aws.port.created.clear()
    run = _executing(
        aws,
        testkit,
        spec=toy.toy_spec(
            RiskTier.AUTO, fee_build=toy.transfer_ops("fee", request_id_in_body=False)
        ),
    )
    out = adapter_call(run)
    assert out["result"]["status"] == "OUTCOME_UNKNOWN"
    assert len(aws.port.sends) == 1  # one attempt per call; the runner decides on resends


def test_adapter_resend_after_a_crash_reads_back_before_sending(aws, testkit):
    aws.port.crash_times["transfers.create"] = 1
    run = _executing(aws, testkit)
    with pytest.raises(toy.Crash):
        adapter_call(run)
    rid = run["step"]["pending"]["op"]["request_id"]
    from awx_spine_aws.store import DynamoDbOperationStore

    # A send that raises in-process is recorded OUTCOME_UNKNOWN before it propagates.
    assert DynamoDbOperationStore("ops", client=aws.db).get(rid).status == "OUTCOME_UNKNOWN"
    run = step(engine.Phase.CRASHED, run, error={"Error": "Sandbox.Timedout", "Cause": "{}"})
    assert run["step"]["pending"]["action"] == "resend"
    out = adapter_call(run)
    assert out["result"]["status"] == "SUCCEEDED"
    assert len(aws.port.sends) == 1  # found by read-back, never sent again
    assert DynamoDbOperationStore("ops", client=aws.db).get(rid).status == "SUCCEEDED"


def test_adapter_complete_writes_the_runners_result_to_the_store(aws, testkit):
    """An unknown after a crash is written back, never left PENDING."""
    aws.port.crash_times["beneficiaries.create"] = 1
    run = _executing(
        aws,
        testkit,
        spec=toy.toy_spec(
            RiskTier.AUTO, fee_build=toy.transfer_ops("fee", request_id_in_body=False)
        ),
    )
    with pytest.raises(toy.Crash):
        adapter_call(run)
    run = step(engine.Phase.CRASHED, run, error={"Error": "Lambda.Unknown", "Cause": "{}"})
    pending = run["step"]["pending"]
    assert pending["action"] == "complete"
    out = adapter_call(run, result=pending["result"])
    assert out["result"]["status"] == "OUTCOME_UNKNOWN"
    from awx_spine_aws.store import DynamoDbOperationStore

    rec = DynamoDbOperationStore("ops", client=aws.db).get(pending["op"]["request_id"])
    assert rec.status == "OUTCOME_UNKNOWN"
    run = step(engine.Phase.COMPLETED, run, result=out["result"])
    assert run["state"] == "FAILED"
    assert run["results"]["fee"][0]["status"] == "OUTCOME_UNKNOWN"
    assert len(aws.port.sends) == 1


def test_adapter_refuses_a_child_whose_parent_is_not_approved(aws, testkit):
    use_spec(testkit, toy.toy_spec(RiskTier.SINGLE))
    pending = start()  # a parent still waiting for approval
    use_spec(testkit, toy.toy_spec(RiskTier.AUTO))
    child = start(raw={"payee": "ben_2", "amount": "1.00"})
    aws.db.update_item(
        TableName="proposals",
        Key={"proposal_id": {"S": pid(child)}},
        UpdateExpression="SET parent_proposal_id = :p",
        ExpressionAttributeValues={":p": {"S": pid(pending)}},
    )
    run = step(engine.Phase.BUILD, step(engine.Phase.RECHECK, child), step="fee")
    with pytest.raises(adapter.NotAuthorised, match="parent"):
        adapter_call(run)
    assert aws.port.sends == []


# --- approval API --------------------------------------------------------------------------


def http(
    method,
    proposal_id,
    *,
    sub="approver-sub",
    tenant="platform",
    groups="[approver]",
    body=None,
    query=None,
    token_use="access",  # noqa: S107 - a JWT claim value, not a secret
):
    if proposal_id is None:
        route = "GET /proposals"
    elif method == "POST":
        route = "POST /proposals/{proposal_id}/decision"
    else:
        route = "GET /proposals/{proposal_id}"
    claims = {"sub": sub, "tenant_id": tenant, "cognito:groups": groups, "token_use": token_use}
    return {
        "routeKey": route,
        "queryStringParameters": query,
        "pathParameters": {"proposal_id": proposal_id} if proposal_id else None,
        "body": None if body is None else json.dumps(body),
        "requestContext": {"http": {"method": method}, "authorizer": {"jwt": {"claims": claims}}},
    }


def waiting(aws, testkit):
    run = start()
    step(engine.Phase.REQUEST_APPROVAL, run, token="tok-1", execution_arn=f"arn:exec:{pid(run)}")
    return run


def test_approval_get_shows_the_exact_intent_and_amounts(aws, testkit):
    run = waiting(aws, testkit)
    resp = approval.handler(http("GET", pid(run)), None)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["status"] == "PENDING"
    assert body["proposal"]["intent_hash"] == run["proposal"]["intent_hash"]
    assert body["proposal"]["computed"]["total"] == {"$money": ["101.50", "AUD"]}


def test_approval_list_is_scoped_to_the_callers_tenant_and_filters_by_status(aws, testkit):
    run = waiting(aws, testkit)
    mine = json.loads(approval.handler(http("GET", None), None)["body"])["proposals"]
    assert [v["proposal"]["proposal_id"] for v in mine] == [pid(run)]
    other = json.loads(approval.handler(http("GET", None, tenant="tenant-a"), None)["body"])
    assert other["proposals"] == []
    done = approval.handler(http("GET", None, query={"status": "SUCCEEDED"}), None)
    assert json.loads(done["body"])["proposals"] == []
    pending = approval.handler(http("GET", None, query={"status": "PENDING"}), None)
    assert len(json.loads(pending["body"])["proposals"]) == 1
    bad = approval.handler(http("GET", None, query={"status": "x;y"}), None)
    assert bad["statusCode"] == 400


def test_approve_sends_the_task_token(aws, testkit):
    run = waiting(aws, testkit)
    body = {"decision": "APPROVE", "intent_hash": run["proposal"]["intent_hash"]}
    resp = approval.handler(http("POST", pid(run), body=body), None)
    assert resp["statusCode"] == 200
    assert aws.sfn.sent == [("tok-1", json.dumps({"approval": "APPROVED"}))]


@pytest.mark.parametrize(
    ("kwargs", "status"),
    [
        ({"sub": "requester-sub"}, 409),  # the proposer approving their own proposal
        ({"groups": "[requester]"}, 403),
        ({"tenant": "tenant-b"}, 404),  # other tenants cannot see it
        ({"body": {"decision": "APPROVE", "intent_hash": "0" * 64}}, 409),  # wrong hash
        ({"body": {"decision": "MAYBE", "intent_hash": "0" * 64}}, 400),
        ({"sub": ""}, 401),
        ({"tenant": ""}, 401),
        ({"token_use": "id"}, 401),  # access tokens only
    ],
)
def test_approval_refusals_store_nothing_and_send_nothing(aws, testkit, kwargs, status):
    run = waiting(aws, testkit)
    default = {"decision": "APPROVE", "intent_hash": run["proposal"]["intent_hash"]}
    body = kwargs.pop("body", default)
    resp = approval.handler(http("POST", pid(run), body=body, **kwargs), None)
    assert resp["statusCode"] == status
    assert aws.sfn.sent == []
    assert DecisionRepo("decisions", client=aws.db).list(pid(run)) == []


def test_a_second_decision_by_the_same_approver_is_refused(aws, testkit):
    use_spec(testkit, toy.toy_spec(RiskTier.DUAL))
    run = waiting(aws, testkit)
    body = {"decision": "APPROVE", "intent_hash": run["proposal"]["intent_hash"]}
    assert approval.handler(http("POST", pid(run), body=body), None)["statusCode"] == 202
    assert approval.handler(http("POST", pid(run), body=body), None)["statusCode"] == 409
    assert aws.sfn.sent == []
    second = approval.handler(http("POST", pid(run), sub="approver-2", body=body), None)
    assert second["statusCode"] == 200
    assert len(aws.sfn.sent) == 1


def test_dual_race_the_second_writer_sends_the_token(aws, testkit, monkeypatch):
    """Both approvers read one decision before either writes; the one who
    writes second re-reads and sends the token."""
    use_spec(testkit, toy.toy_spec(RiskTier.DUAL))
    run = waiting(aws, testkit)
    p = ProposalRepo("proposals", client=aws.db).get(pid(run)).proposal
    DecisionRepo("decisions", client=aws.db).put(
        ApprovalDecision(
            p.proposal_id,
            p.intent_hash,
            "approver-1",
            Decision.APPROVE,
            p.created_at + timedelta(seconds=1),
        )
    )
    real = DecisionRepo.list
    calls = {"n": 0}

    def stale_first(self, proposal_id):
        calls["n"] += 1
        items = real(self, proposal_id)
        return [] if calls["n"] == 1 else items  # the first read misses approver-1

    monkeypatch.setattr(DecisionRepo, "list", stale_first)
    body = {"decision": "APPROVE", "intent_hash": p.intent_hash}
    resp = approval.handler(http("POST", pid(run), sub="approver-2", body=body), None)
    assert resp["statusCode"] == 200
    assert len(aws.sfn.sent) == 1


def test_groups_claim_formats():
    assert approval._groups("[approver requester]") == {"approver", "requester"}
    assert approval._groups(["approver"]) == {"approver"}
    assert approval._groups('["approver","x"]') == {"approver", "x"}
    assert approval._groups(None) == set()


def test_pre_token_trigger_puts_tenant_id_in_the_access_token():
    event = {"request": {"userAttributes": {"custom:tenant_id": "tenant-a"}}, "response": {}}
    out = pre_token.handler(event, None)
    access = out["response"]["claimsAndScopeOverrideDetails"]["accessTokenGeneration"]
    assert access["claimsToAddOrOverride"] == {"tenant_id": "tenant-a"}
    bare = pre_token.handler({"request": {"userAttributes": {}}, "response": {}}, None)
    assert "claimsAndScopeOverrideDetails" not in bare["response"]


def test_proposal_outcome_attribute_is_pinned_for_the_agent_plane(aws, testkit):
    """The agent plane's tool Lambda reads only proposal_id and outcome (Select
    SPECIFIC_ATTRIBUTES). The name, the values and the absence of the task token from that
    projection are part of the contract."""
    run = start()
    step(engine.Phase.REQUEST_APPROVAL, run, token="secret-task-token")
    item = aws.db.get_item(
        TableName="proposals",
        Key={"proposal_id": {"S": pid(run)}},
        ProjectionExpression="proposal_id, outcome",
    )["Item"]
    assert set(item) == {"proposal_id"}  # no outcome until the run ends; never the token
    final = step(engine.Phase.AUDIT, step(engine.Phase.BINDING, run))
    assert final["state"] == "EXPIRED"
    item = aws.db.get_item(
        TableName="proposals",
        Key={"proposal_id": {"S": pid(run)}},
        ProjectionExpression="proposal_id, outcome",
    )["Item"]
    assert item == {"proposal_id": {"S": pid(run)}, "outcome": {"S": "EXPIRED"}}
    assert "secret-task-token" not in json.dumps(item)
    full = aws.db.get_item(TableName="proposals", Key={"proposal_id": {"S": pid(run)}})["Item"]
    assert full["task_token"]["S"] == "secret-task-token"  # its own attribute, not in `outcome`


def test_outcome_values_are_approval_states_or_run_outcomes():
    from awx_common.approvals import ApprovalState

    allowed = {s.value for s in ApprovalState} | {o.value for o in engine.Outcome}
    assert allowed == {
        "NOT_REQUIRED",
        "PENDING",
        "APPROVED",
        "REJECTED",
        "EXPIRED",
        "BLOCKED",
        "SUCCEEDED",
        "FAILED",
        "POSTCONDITION_FAILED",
    }


def test_a_token_send_that_fails_on_a_timed_out_wait_is_410(aws, testkit):
    """The wait ended at its timeout, so the payment will never run."""
    run = waiting(aws, testkit)  # SINGLE
    aws.sfn.fail_sends = [aws.sfn.exceptions.TaskTimedOut]
    aws.sfn.status = "TIMED_OUT"
    body = {"decision": "APPROVE", "intent_hash": run["proposal"]["intent_hash"]}
    resp = approval.handler(http("POST", pid(run), body=body), None)
    assert resp["statusCode"] == 410
    assert aws.sfn.described == [f"arn:exec:{pid(run)}"]


@pytest.mark.parametrize("status", ["ABORTED", "FAILED", "SUCCEEDED"])
def test_a_token_send_that_fails_on_a_stopped_execution_is_410(aws, testkit, status):
    run = waiting(aws, testkit)
    aws.sfn.fail_sends = [aws.sfn.exceptions.InvalidToken]
    aws.sfn.status = status
    body = {"decision": "APPROVE", "intent_hash": run["proposal"]["intent_hash"]}
    assert approval.handler(http("POST", pid(run), body=body), None)["statusCode"] == 410


def test_a_failed_send_on_a_running_execution_with_an_outcome_is_410(aws, testkit):
    run = waiting(aws, testkit)

    def run_ended():  # the run records its outcome just before this send
        ProposalRepo("proposals", client=aws.db).set_outcome(pid(run), "EXPIRED")

    aws.sfn.before_send = run_ended
    aws.sfn.fail_sends = [aws.sfn.exceptions.TaskTimedOut]
    body = {"decision": "APPROVE", "intent_hash": run["proposal"]["intent_hash"]}
    assert approval.handler(http("POST", pid(run), body=body), None)["statusCode"] == 410


def test_two_approvers_racing_on_a_running_execution_both_get_200(aws, testkit):
    """Both approvers write before either re-reads: approver 1's write lands (its request is
    then paused), approver 2 decides, sees APPROVED and sends first. Approver 1 resumes at the
    re-read and send, through the same code the handler runs; its send fails on the used
    token, the execution is RUNNING, so both are told APPROVED. No repository patching."""
    use_spec(testkit, toy.toy_spec(RiskTier.DUAL))
    run = waiting(aws, testkit)
    repo = ProposalRepo("proposals", client=aws.db)
    p = repo.get(pid(run)).proposal
    DecisionRepo("decisions", client=aws.db).put(  # approver 1's write, request paused here
        ApprovalDecision(
            p.proposal_id,
            p.intent_hash,
            "approver-1",
            Decision.APPROVE,
            p.created_at + timedelta(seconds=1),
        )
    )
    body = {"decision": "APPROVE", "intent_hash": p.intent_hash}
    second = approval.handler(http("POST", pid(run), sub="approver-2", body=body), None)
    first = approval._send_and_report(repo.get(pid(run)), p.created_at + timedelta(seconds=2))
    assert second["statusCode"] == 200
    assert first["statusCode"] == 200
    assert json.loads(first["body"]) == json.loads(second["body"]) == {"status": "APPROVED"}
    assert len(aws.sfn.sent) == 1
    assert aws.sfn.described == [f"arn:exec:{pid(run)}"]
