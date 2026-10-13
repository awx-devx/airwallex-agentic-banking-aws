"""The KitWorkflow state machine and local_runner reach the same outcome.

This interprets the ASL the KitWorkflow construct actually synthesises (States, Next, Choice
rules, Retry, Catch, ResultPath, Map and child executions) and runs every Task against the
real Lambda handlers on moto tables. Each case then runs the same spec through
local_runner and compares the outcome, the per-step result statuses, the order of kit
callbacks and the operation-store status of every request_id. It also checks which states the
execution visited, so the catch and route paths are proven, not assumed.
"""

from __future__ import annotations

import json
import sys
import types
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from awx_common import Money, Origin, RiskTier
from awx_common.approvals import Decision
from awx_common.store import InMemoryKitStore
from awx_common.tenancy import TenantContext
from awx_common.transport import encode
from awx_spine.store import InMemoryOperationStore
from awx_spine_aws import engine
from awx_spine_aws.handlers import adapter, kit_step
from awx_spine_aws.local_runner import run_kit_workflow
from awx_spine_aws.repos import DecisionRepo, ProposalRepo
from awx_spine_aws.store import DynamoDbOperationStore

import toy
from aws_harness import MODULE, sfn_json

pytestmark = pytest.mark.synth

ACTIONS = ("pay_supplier", "pay_many")


# --- synthesis ------------------------------------------------------------------------------


def _shape_specs() -> dict[str, Any]:
    """Specs with the same structure as every case below (the ASL depends only on that)."""
    return {"pay_supplier": toy.toy_spec(), "pay_many": toy.fan_out_spec()}


@pytest.fixture(scope="module")
def machines():
    """{action: {"root": definition, "child": definition | None}} from a real synth."""
    import aws_cdk as cdk
    from aws_cdk.assertions import Template

    root = __import__("pathlib").Path(__file__).resolve().parents[3]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from awx_common.regions import DEFAULT_REGION
    from infra.app import build_app
    from infra.constructs.kit_workflow import KitWorkflow
    from infra.kit_manifest import parse_workflow

    module = types.ModuleType(MODULE)
    module.SPECS = _shape_specs()
    saved = sys.modules.get(MODULE)
    sys.modules[MODULE] = module
    try:
        app = cdk.App(context={"account": "111122223333", "region": DEFAULT_REGION})
        env = cdk.Environment(account="111122223333", region=DEFAULT_REGION)
        build_app(app)
        spine = app.node.find_child("AwxSpine")
        out = {}
        stacks = {}
        for action in ACTIONS:
            stack = cdk.Stack(app, f"Asl{action.title().replace('_', '')}", env=env)
            KitWorkflow(
                stack,
                "Wf",
                spine=spine,
                kit="k3",
                action=action,
                spec_module=MODULE,
                workflow=parse_workflow(module.SPECS[action]),
                allow_stand_in=True,  # MODULE is an in-memory stand-in
            )
            stacks[action] = stack
        for action, stack in stacks.items():
            template = Template.from_stack(stack).to_json()
            defs = {}
            for lid, res in template["Resources"].items():
                if res["Type"] == "AWS::StepFunctions::StateMachine":
                    defs[lid] = _render(res["Properties"]["DefinitionString"])
            child = next((d for lid, d in defs.items() if "ChildMachine" in lid), None)
            rootdef = next(d for lid, d in defs.items() if "RootMachine" in lid)
            out[action] = {"root": rootdef, "child": child}
        return out
    finally:
        if saved is None:
            sys.modules.pop(MODULE, None)
        else:
            sys.modules[MODULE] = saved


def _token(part: Any) -> str:
    if isinstance(part, str):
        return part
    if "Fn::GetAtt" in part:
        return f"<{part['Fn::GetAtt'][0]}>"
    if "Ref" in part:
        return "aws" if part["Ref"] == "AWS::Partition" else f"<{part['Ref']}>"
    if "Fn::ImportValue" in part:
        return f"<import:{part['Fn::ImportValue']}>".replace('"', "")
    raise AssertionError(f"unexpected intrinsic {part}")


def _render(definition: Any) -> dict[str, Any]:
    parts = definition["Fn::Join"][1]
    return json.loads("".join(_token(p) for p in parts))


# --- a small ASL interpreter ----------------------------------------------------------------


class TaskFailed(Exception):
    def __init__(self, error: str, cause: str) -> None:
        super().__init__(error)
        self.error, self.cause = error, cause


def _get(path: str, data: Any, ctx: Mapping[str, Any]) -> Any:
    root: Any = ctx if path.startswith("$$") else data
    rest = path[2:] if path.startswith("$$") else path[1:]
    for part in [p for p in rest.split(".") if p]:
        root = root[part]
    return root


def _resolve(params: Any, data: Any, ctx: Mapping[str, Any]) -> Any:
    if isinstance(params, dict):
        out = {}
        for key, value in params.items():
            if key.endswith(".$"):
                out[key[:-2]] = _get(value, data, ctx)
            else:
                out[key] = _resolve(value, data, ctx)
        return out
    if isinstance(params, list):
        return [_resolve(v, data, ctx) for v in params]
    return params


def _set(path: str | None, data: Any, value: Any) -> Any:
    if path is None or path == "$":
        return value
    out = json.loads(json.dumps(data))
    target = out
    parts = path[2:].split(".")
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = value
    return out


def _matches(rule: Mapping[str, Any], data: Any) -> bool:
    if "And" in rule:
        return all(_matches(r, data) for r in rule["And"])
    if "Or" in rule:
        return any(_matches(r, data) for r in rule["Or"])
    if "Not" in rule:
        return not _matches(rule["Not"], data)
    try:
        value = _get(rule["Variable"], data, {})
        present = True
    except (KeyError, TypeError):
        value, present = None, False
    for op, want in rule.items():
        if op == "IsString":
            return present and isinstance(value, str) == want
        if op == "IsNull":
            return present and (value is None) == want
        if op == "IsPresent":
            return present == want
        if op == "StringEquals":
            return present and value == want
        if op == "BooleanEquals":
            return present and value is want
        if op == "NumericEquals":
            return present and value == want
    raise AssertionError(f"unsupported Choice rule {rule}")


class Interpreter:
    def __init__(
        self, machines: Mapping[str, Any], decide: Callable, aws: Any, dies: frozenset = frozenset()
    ) -> None:
        self.machines, self.decide, self.aws = machines, decide, aws
        self.dies = dies  # states whose Lambda dies (infrastructure, not kit code)
        self.visited: list[str] = []
        self.on_wait: Callable[[float], None] | None = None

    def execute(self, which: str, data: Any, name: str) -> Any:
        definition = self.machines[which]
        ctx = {"Execution": {"Input": data, "Name": name, "Id": f"exec:{name}"}}
        return self._states(definition, definition["StartAt"], data, ctx)

    def _states(self, block: Mapping[str, Any], cur: str, data: Any, ctx: dict) -> Any:
        while True:
            state = block["States"][cur]
            self.visited.append(cur)
            kind = state["Type"]
            if kind == "Succeed":
                return data
            if kind == "Fail":
                raise TaskFailed(state.get("Error", "Fail"), state.get("Cause", ""))
            if kind == "Choice":
                cur = next(
                    (r["Next"] for r in state["Choices"] if _matches(r, data)), state["Default"]
                )
                continue
            if kind == "Wait":
                assert "Seconds" in state or "SecondsPath" in state
                if self.on_wait is not None:  # a fake clock sees the wait
                    secs = state.get("Seconds")
                    self.on_wait(
                        secs if secs is not None else _get(state["SecondsPath"], data, ctx)
                    )
                cur = state["Next"]
                continue
            if kind == "Pass":
                value = _resolve(state.get("Parameters", data), data, ctx)
                data = _set(state.get("ResultPath"), data, value)
            elif kind in ("Task", "Map"):
                try:
                    raw = (
                        self._task(state, data, ctx)
                        if kind == "Task"
                        else self._map(state, data, ctx)
                    )
                except TaskFailed as failed:
                    nxt = self._catch(state, failed)
                    if nxt is None:
                        raise
                    target, result_path = nxt
                    err = {"Error": failed.error, "Cause": failed.cause}
                    data = _set(result_path, data, err)
                    cur = target
                    continue
                if "ResultSelector" in state:
                    raw = _resolve(state["ResultSelector"], raw, ctx)
                data = _set(state.get("ResultPath", "$"), data, raw)
            else:
                raise AssertionError(f"unsupported state type {kind}")
            if state.get("End"):
                return data
            cur = state["Next"]

    @staticmethod
    def _catch(state: Mapping[str, Any], failed: TaskFailed) -> tuple[str, str | None] | None:
        for catcher in state.get("Catch", []):
            names = catcher["ErrorEquals"]
            if failed.error in names or "States.ALL" in names:
                return catcher["Next"], catcher.get("ResultPath")
        return None

    def _retries(self, state: Mapping[str, Any], error: str, attempt: int) -> bool:
        for retrier in state.get("Retry", []):
            if error in retrier["ErrorEquals"] or "States.ALL" in retrier["ErrorEquals"]:
                return attempt < retrier.get("MaxAttempts", 3)
        return False

    def _task(self, state: Mapping[str, Any], data: Any, ctx: dict) -> Any:
        resource = state["Resource"]
        if resource.endswith("startExecution.sync:2"):
            params = _resolve(state["Parameters"], data, ctx)
            child_input = dict(params["Input"])
            child_input.pop("AWS_STEP_FUNCTIONS_STARTED_BY_EXECUTION_ID", None)
            return {"Output": self.execute("child", child_input, params["Name"])}
        if resource.endswith("lambda:invoke.waitForTaskToken"):
            params = _resolve(state["Parameters"], data, {**ctx, "Task": {"Token": "tok"}})
            self._invoke(params["FunctionName"], params["Payload"])
            pid = params["Payload"]["run"]["proposal"]["proposal_id"]
            proposal = ProposalRepo("proposals", client=self.aws.db).get(pid).proposal
            decisions = self.decide(proposal)
            if not decisions:
                raise TaskFailed("States.Timeout", "approval wait timed out")
            for d in decisions:
                DecisionRepo("decisions", client=self.aws.db).put(d)
            return {"approval": "signalled"}
        if self.visited[-1] in self.dies:
            raise TaskFailed("Lambda.Unknown", "the function's runtime exited")
        attempt = 0
        while True:
            try:
                return self._invoke(resource, _resolve(state["Parameters"], data, ctx))
            except TaskFailed as failed:
                attempt += 1
                if not self._retries(state, failed.error, attempt):
                    raise

    def _invoke(self, function: str, payload: Any) -> Any:
        handler = adapter.handler if "AdapterFn" in function else kit_step.handler
        assert "AdapterFn" in function or "StepFn" in function, function
        try:
            return sfn_json(handler(sfn_json(payload), None))
        except Exception as exc:
            cause = json.dumps({"errorType": type(exc).__name__, "errorMessage": str(exc)})
            raise TaskFailed(type(exc).__name__, cause) from None

    def _map(self, state: Mapping[str, Any], data: Any, ctx: dict) -> Any:
        items = _get(state["ItemsPath"], data, ctx)
        processor = state["ItemProcessor"]
        out = []
        for item in items:
            sub = {**ctx, "Map": {"Item": {"Value": item}}}
            start = processor["StartAt"]
            out.append(self._states(processor, start, item, sub))
        return out


# --- the cases ------------------------------------------------------------------------------


def _spec(case: Mapping[str, Any]):
    if case.get("fan_out"):
        return toy.fan_out_spec(child_tiers=case.get("child_tiers"))
    spec = toy.toy_spec(
        case.get("tier", RiskTier.AUTO),
        fee_build=case.get("fee_build"),
        pay_build=case.get("pay_build"),
        reconcile=case.get("reconcile", False),
        sandbox_finality=case.get("sandbox", False),
    )
    if case.get("postcondition_raises"):

        def boom(ctx):
            raise KeyError("ledger")

        spec = type(spec)(**{**spec.__dict__, "postcondition": boom})
    return spec


def _port(case: Mapping[str, Any]) -> toy.ToyPort:
    return toy.ToyPort(
        final_status=case.get("final", "PAID"),
        auto_move=not case.get("sandbox", False),
        crash_times=Counter(case.get("crash", {})),
        unknown_times=Counter(case.get("unknown", {})),
        rate_limit_times=Counter(case.get("rate_limit", {})),
        hide_list_reads=case.get("hide_list", 0),
        refuse_duplicates=Counter(case.get("refuse", {})),
        rate_limit_duplicates=Counter(case.get("rate_limit_dup", {})),
    )


def _decide(case: Mapping[str, Any], port_ref: Callable[[], toy.ToyPort]):
    kind = case.get("decide")
    if kind is None:
        return toy.no_decision
    if kind == "reject":
        return toy.approve_by("approver", decision=Decision.REJECT)
    if kind == "drain":

        def drain(p):
            port_ref().balance = Money.of("50.00", "AUD")
            return toy.approve_by("approver")(p)

        return drain
    return toy.approve_by(*kind)


def _broken(ctx):
    raise ValueError("bad build")


CASES = {
    "auto": {},
    "single-approved": {"tier": RiskTier.SINGLE, "decide": ("approver",)},
    "rejected": {"tier": RiskTier.SINGLE, "decide": "reject"},
    "expired-timeout": {"tier": RiskTier.SINGLE},
    "stale": {"tier": RiskTier.SINGLE, "decide": "drain"},
    "agent-floor": {"origin": Origin.AGENT, "decide": ("approver",)},
    "crash-reconciled": {"crash": {"transfers.create": 1}, "reconcile": True},
    "crash-resent": {"crash": {"transfers.create": 1}},
    "crash-no-dedupe": {
        "crash": {"beneficiaries.create": 1},
        "fee_build": toy.transfer_ops("fee", request_id_in_body=False),
    },
    "unknown-no-dedupe": {
        "unknown": {"beneficiaries.create": 1},
        "fee_build": toy.transfer_ops("fee", request_id_in_body=False),
    },
    "rate-limited": {"rate_limit": {"transfers.create": 1}},
    # The first send lands unanswered, the read-back lags, the resend is refused. The
    # deployed machine must treat the resend's OUTCOME_UNKNOWN like a send's.
    "unknown-resend-refused": {
        "unknown": {"transfers.create": 1},
        "hide_list": 99,
        "refuse": {"transfers.create": 1},
    },
    # Unknown, then a 429 on every resend until MAX_RESENDS is spent.
    "unknown-resend-rate-limited": {
        "unknown": {"transfers.create": 1},
        "hide_list": 99,
        "rate_limit_dup": {"transfers.create": 99},
    },
    "build-raises": {"fee_build": _broken},
    "postcondition-raises": {"postcondition_raises": True},
    "finality-cancelled": {"final": "CANCELLED"},
    "sandbox-advances": {"sandbox": True},
    "build-lambda-dies": {"fee_build": _broken, "dies": {"Build1Fee"}},
    "postcondition-lambda-dies": {"postcondition_raises": True, "dies": {"Postcondition"}},
    "empty-step": {"fee_build": lambda ctx: ()},
    "reconcile-mismatch": {
        "crash": {"transfers.create": 1},
        "reconcile": toy.reconcile_wrong_amount,
    },
    "empty-finality-step": {"pay_build": lambda ctx: ()},
    "f8-unknown-landed": {"seed": {"landed": True}},
    "f8-unknown-not-landed": {"seed": {"landed": False}},
    "f8-unknown-no-dedupe": {
        "seed": {"landed": False, "rid_in_body": False},
        "fee_build": toy.transfer_ops("fee", request_id_in_body=False),
    },
    "fan-out-blocked-child": {
        "fan_out": True,
        "decide": ("approver",),
        "child_tiers": {"ben_2": RiskTier.BLOCKED},
        "raw": {"payees": ["ben_1", "ben_2"], "amount": "10.00"},
    },
}

MUST_VISIT = {
    "expired-timeout": {"RequestApproval", "Binding"},
    "crash-reconciled": {"Crashed1Fee", "AdapterComplete1Fee", "Completed1Fee"},
    "crash-resent": {"Crashed1Fee", "ResendWait1Fee"},
    "crash-no-dedupe": {"Crashed1Fee", "AdapterComplete1Fee", "Postcondition"},
    "rate-limited": {"ResendWait1Fee"},
    "unknown-resend-refused": {"ResendWait1Fee", "Postcondition"},
    "unknown-resend-rate-limited": {"ResendWait1Fee", "Postcondition"},
    "build-raises": {"AfterStop", "Postcondition"},  # rule 7: caught inside the Lambda
    "build-lambda-dies": {"Error", "AfterStop", "Postcondition"},
    "postcondition-lambda-dies": {"PostconditionError", "Audit"},
    "sandbox-advances": {"FinalityWait2Pay", "FinalityCheck2Pay", "Adapter2Pay"},
    "fan-out-blocked-child": {"FanOut", "Children", "StartChild", "ChildrenDone"},
    "stale": {"Recheck", "AfterStop"},
    "empty-step": {"Build1Fee", "Route1Fee", "Build2Pay"},
    "reconcile-mismatch": {"Crashed1Fee", "AdapterComplete1Fee", "Completed1Fee", "AfterStop"},
    "empty-finality-step": {"Build2Pay", "Route2Pay", "Postcondition"},
}


def _statuses(results: Mapping[str, Any]) -> dict[str, list[str]]:
    return {
        k: [r["status"] if isinstance(r, Mapping) else r.status.value for r in v]
        for k, v in results.items()
    }


@pytest.mark.parametrize("name", list(CASES))
def test_state_machine_and_local_runner_agree(name, aws, machines, monkeypatch):
    case = CASES[name]
    action = "pay_many" if case.get("fan_out") else "pay_supplier"
    raw = case.get("raw", toy.RAW)
    origin = case.get("origin", Origin.SCHEDULE)

    # --- Step Functions, interpreted from the synthesised ASL, against the real handlers
    sfn_calls: list = []
    spec = _spec(case)
    spec = _with_calls(spec, sfn_calls)
    module = types.ModuleType(MODULE)
    module.SPECS = {action: spec}
    monkeypatch.setitem(sys.modules, MODULE, module)
    monkeypatch.setenv("AWX_ACTION", action)
    kit_step.root_spec.cache_clear()
    aws.port = _port(case)
    sfn_store = DynamoDbOperationStore("ops", client=aws.db)
    local_port = _port(case)
    local_store = InMemoryOperationStore()
    if "seed" in case:  # kit rule M1: an earlier run left the fee at OUTCOME_UNKNOWN
        for store, port in ((sfn_store, aws.port), (local_store, local_port)):
            toy.seed_unknown(
                store,
                port,
                landed=case["seed"]["landed"],
                request_id_in_body=case["seed"].get("rid_in_body", True),
            )
    lenv = kit_step.LambdaEnv()
    tenant = lenv.resolve_tenant("platform")
    proposal, decision = engine.propose(
        spec, (), raw, proposed_by="requester-sub", tenant=tenant, env=lenv, origin=origin
    )
    data = sfn_json(encode(engine.execution_input(proposal, decision, raw, None)))
    dies = frozenset(case.get("dies", ()))
    interp = Interpreter(machines[action], _decide(case, lambda: aws.port), aws, dies)
    final = interp.execute("root", data, proposal.proposal_id)["run"]

    # --- local_runner on the same spec and the same faults
    local_calls: list = []
    local = run_kit_workflow(
        _with_calls(_spec(case), local_calls),
        raw,
        proposed_by="requester-sub",
        tenant=TenantContext("platform"),
        port=local_port,
        store=local_store,
        decide=_decide(case, lambda: local_port),
        now=lambda: datetime.now(UTC),
        sleep=lambda s: None,
        kit_store=InMemoryKitStore(),
        origin=origin,
    )

    assert final["state"] == local.state, (final["detail"], local.detail)
    assert _statuses(final["results"]) == _statuses(local.results)
    if not dies:  # a dead Lambda never reached the kit callback the local run raised in
        assert [c[0] for c in sfn_calls] == [c[0] for c in local_calls]
    rids = {op.request_id for op in local_port.sends} | {op.request_id for op in aws.port.sends}
    for rid in rids:
        local_rec, sfn_rec = local_store.get(rid), sfn_store.get(rid)
        assert (local_rec and local_rec.status) == (sfn_rec and sfn_rec.status), rid
    assert len(aws.port.sends) == len(local_port.sends)
    if name in ("unknown-resend-refused", "unknown-resend-rate-limited"):
        fee = toy.fee_op().request_id
        for res in (final["results"]["fee"][-1]["status"], local.results["fee"][-1].status.value):
            assert res == "OUTCOME_UNKNOWN"
        for rec in (local_store.get(fee), sfn_store.get(fee)):
            assert rec.status == "OUTCOME_UNKNOWN"
            assert rec.may_have_landed
        assert len(local_port.created) == len(aws.port.created) == 1
    if "seed" in case:
        fee_rid = toy.fee_op().request_id
        expected = (
            1 if case["seed"]["landed"] is False and case["seed"].get("rid_in_body", True) else 0
        )
        for port in (aws.port, local_port):
            assert len([o for o in port.sends if o.request_id == fee_rid]) == expected
    missing = MUST_VISIT.get(name, set()) - set(interp.visited)
    assert not missing, f"states not visited: {sorted(missing)}"
    audit_states = {i["state"]["S"] for i in aws.db.scan(TableName="audit")["Items"]}
    assert final["state"] in audit_states


def _with_calls(spec, calls: list):
    """Record every kit callback, in order, without changing what the spec does."""

    def wrap(name, fn):
        if fn is None:
            return None

        def inner(*args, **kwargs):
            calls.append((name,))
            return fn(*args, **kwargs)

        return inner

    steps = tuple(
        type(s)(s.name, wrap(f"build:{s.name}", s.build), s.finality, s.reconcile)
        for s in spec.steps
    )
    child = _with_calls(spec.child, calls) if spec.child is not None else None
    return type(spec)(
        **{
            **spec.__dict__,
            "steps": steps,
            "postcondition": wrap("postcondition", spec.postcondition),
            "on_abandon": wrap("on_abandon", spec.on_abandon),
            "child": child,
        }
    )


def test_every_spine_state_type_is_interpreted(machines):
    """Guard: the interpreter knows every state type and Choice operator the construct emits."""
    seen_types, seen_ops = set(), set()
    for pair in machines.values():
        for definition in filter(None, pair.values()):
            stack = [definition]
            while stack:
                block = stack.pop()
                for state in block["States"].values():
                    seen_types.add(state["Type"])
                    if state["Type"] == "Map":
                        stack.append(state["ItemProcessor"])
                    for rule in state.get("Choices", []):
                        seen_ops |= {k for k in rule if k not in ("Variable", "Next")}
    assert seen_types <= {"Task", "Choice", "Wait", "Pass", "Map", "Succeed", "Fail"}
    assert seen_ops <= {"IsString", "IsNull", "StringEquals", "BooleanEquals", "NumericEquals"}


_ = timedelta  # kept for readers extending the cases with approval windows
