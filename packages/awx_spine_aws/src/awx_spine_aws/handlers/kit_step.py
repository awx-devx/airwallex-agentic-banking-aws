"""Kit-step Lambda: runs one engine phase for one kit's KitWorkflow state machine.

AWX_SPEC_MODULE names the kit module exporting SPECS and AWX_ACTION the key; the construct
bundles that module with this code. The event is {"phase": ..., "run": ..., ...} and the
reply is the updated run (awx_common.transport JSON). This role holds no Airwallex secret and
may not invoke the adapter: reads go through the reader Lambda, and operations are only built
here and sent by the state machine to the adapter.

Execution input (the agent plane builds the same), with the execution name equal to the
proposal ID from approvals.derive_proposal_id: transport.dumps({"proposal": Proposal,
"raw_input": args, "tenant_id": str or null, "decision": PolicyDecision, "origin": Origin}).
A child execution adds "parent_proposal_id" and "spec_path". AWX_SANDBOX_MODE=1 turns on
rule 10.

Rule 7: an exception from kit code in any phase after start fails the run closed, with no
retry, and its type and message land in the audit record. Start itself fails the execution
when the proposal it was handed is not intact or not for this spec, after writing a minimal
audit row so the refusal is on record.
"""

from __future__ import annotations

import functools
import importlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from awx_common.errors import AwxError, TenantScopeViolation, TransientServerError, error_class
from awx_common.ops import ReadRequest
from awx_common.store import KitStore
from awx_common.tenancy import TenantContext
from awx_common.transport import decode, encode
from awx_common.workflow import RUN_RUNNING, RUN_TERMINAL, KitWorkflowSpec, approver_tenant

from .. import codec, engine
from ..engine import Phase
from ..kit_store import DynamoDbKitStore
from ..repos import AuditRepo, DecisionRepo, DynamoDbTenantResolver, ProposalRepo
from ._common import boto_client, env, log


class ReadFailed(AwxError):
    """The reader Lambda failed for a reason that is not an Airwallex error."""


@functools.cache
def root_spec() -> KitWorkflowSpec:
    module = importlib.import_module(env("AWX_SPEC_MODULE"))
    return module.SPECS[env("AWX_ACTION")]


# The reader Lambda's timeout (spine_lambda.py's default, which reader_for keeps; a synth test
# pins it) plus the invoke's own overhead: a retry starts only if a whole read still fits.
READER_TIMEOUT_S = 30.0
INVOKE_OVERHEAD_S = 5.0
# botocore errors on the invoke itself that are worth a retry.
_INVOKE_THROTTLES = frozenset(
    {"TooManyRequestsException", "ThrottlingException", "ServiceException", "EC2ThrottledException"}
)
_INVOKE_TIMEOUTS = frozenset({"ReadTimeoutError", "ConnectTimeoutError", "ConnectionClosedError"})


class LambdaEnv:
    """context is the Lambda context (None in tests): read retries never wait past its
    remaining time minus a margin for the reader call itself."""

    MARGIN_S = READER_TIMEOUT_S + INVOKE_OVERHEAD_S

    def __init__(self, context: Any = None) -> None:
        self._context = context

    def sleep(self, seconds: float) -> None:
        import time

        time.sleep(seconds)

    def read_budget_s(self) -> float | None:
        remaining = getattr(self._context, "get_remaining_time_in_millis", None)
        if remaining is None:
            return None
        return max(remaining() / 1000.0 - self.MARGIN_S, 0.0)

    @property
    def sandbox(self) -> bool:
        import os

        return os.environ.get("AWX_SANDBOX_MODE", "1") == "1"

    def now(self) -> datetime:
        return datetime.now(UTC)

    def read(self, kit: str, req: ReadRequest, tenant: TenantContext) -> Mapping[str, Any]:
        return self._invoke({"kit": kit, "read": req, "tenant_id": tenant.tenant_id})

    def read_platform(self, kit: str, req: ReadRequest, tenant: TenantContext) -> Mapping[str, Any]:
        # Headerless, in the run's own tenant: the reader derives a platform read from that.
        return self._invoke({"kit": kit, "read": req, "tenant_id": tenant.tenant_id})

    def store(self, kit: str, tenant: TenantContext) -> KitStore:
        return DynamoDbKitStore(
            env("AWX_LEDGER_TABLE"),
            kit=kit,
            tenant_id=tenant.tenant_id,
            client=boto_client("dynamodb"),
        )

    def resolve_tenant(self, tenant_id: str) -> TenantContext:
        """Always in this step Lambda's own kit (the root spec's): a tenant row belongs to
        one kit."""
        resolver = _repo(DynamoDbTenantResolver, "AWX_TENANTS_TABLE")
        return resolver.resolve(tenant_id, kit=root_spec().kit)

    def kit_actions(self, kit: str) -> frozenset[str]:
        if kit != root_spec().kit:
            return frozenset()
        return frozenset(_run_status_config()["actions"])

    def run_status(self, kit: str, action: str, proposal_id: str, tenant_id: str) -> str | None:
        """The root run of `action` for proposal_id. DescribeExecution on
        awx-<kit>-<action>; the step role may describe this kit's root machines only.

        None only when there is no such root run. A child proposal, or a proposal for another
        kit or action, raises ValueError. A stopped run reads RUN_RUNNING until it can send
        nothing more: its stopDate is at least the adapter timeout plus a margin in the past
        (an adapter Lambda the stop did not kill), and for an abnormal end (FAILED, TIMED_OUT,
        ABORTED) also the action's child machine timeout (children are stopped best-effort).
        For an action with a child machine, a SUCCEEDED execution is also abnormal
        unless the run's recorded outcome is SUCCEEDED with no lost child, or every lost child
        reads terminal. Any other error fails closed (raises). The tenant is required."""
        if not tenant_id:
            raise ValueError("run_status needs the calling run's tenant")
        cfg = _run_status_config()
        now = self.now()  # one `now` for the whole call
        stored = _repo(ProposalRepo, "AWX_PROPOSALS_TABLE").get(proposal_id)
        if stored is None:
            return None
        if stored.parent_proposal_id is not None:
            raise ValueError("run_status: that is a child proposal's ID; ask for its root run")
        if stored.proposal.kit != kit or stored.proposal.action != action:
            raise ValueError(
                f"run_status: {proposal_id} is a {stored.proposal.kit}.{stored.proposal.action} "
                f"proposal, not {kit}.{action}"
            )
        if stored.tenant_id != tenant_id:
            raise ValueError("run_status: that proposal belongs to another tenant")
        return self._settled(cfg, action, proposal_id, stored, now, depth=0)

    def _settled(
        self,
        cfg: Mapping[str, Any],
        machine: str,
        pid: str,
        stored: Any,
        now: datetime,
        depth: int,
    ) -> str | None:
        """machine is the ARN suffix: the action for a root, `<action>-child` for its child.
        `stored` is that proposal's row (None if it has none)."""
        desc = _describe(f"{cfg['prefix']}{machine}:{pid}")
        if desc is None:
            return None
        if str(desc["status"]) not in RUN_TERMINAL:
            return RUN_RUNNING
        children = int(cfg["child_timeout_s"].get(machine, 0)) > 0
        child_wait = children and not self._children_over(cfg, machine, stored, now, depth)
        return settled_status(desc, cfg, machine, now, child_wait=child_wait)

    def _children_over(
        self, cfg: Mapping[str, Any], machine: str, stored: Any, now: datetime, depth: int
    ) -> bool:
        """Whether this run's children can send nothing more without waiting their timeout.

        Either the run's own recorded outcome is SUCCEEDED and
        it lost no child (every child returned, so every child execution ended), or every lost
        child's execution reads terminal under this same rule. Anything else (an outcome of
        FAILED or BLOCKED, no outcome, no row) proves nothing: a failed ChildrenDone can lose
        the record of a lost child, and the run then always ends FAILED."""
        if stored is None:
            return False
        lost = tuple(stored.lost_children)
        if not lost:
            return stored.outcome == "SUCCEEDED"
        if depth >= 3:
            return False
        repo = _repo(ProposalRepo, "AWX_PROPOSALS_TABLE")
        for child_pid in lost:
            got = self._settled(
                cfg, f"{machine}-child", child_pid, repo.get(child_pid), now, depth + 1
            )
            if got is None or got == RUN_RUNNING:
                return False
        return True

    @staticmethod
    def _invoke(event: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            resp = boto_client("lambda").invoke(
                FunctionName=env("AWX_READER_FUNCTION"),
                Payload=json.dumps(encode(event)).encode(),
            )
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            name = type(exc).__name__
            if code in _INVOKE_THROTTLES or name in _INVOKE_TIMEOUTS:
                raise TransientServerError(f"reader invoke {code or name}: {exc}"[:300]) from exc
            raise
        raw = resp["Payload"].read()
        if resp.get("FunctionError"):
            err = json.loads(raw or b"{}")
            name = err.get("errorType", "")
            message, fields = codec.error_fields(str(err.get("errorMessage", "read failed")))
            try:
                cls = error_class(name)
            except KeyError:
                raise ReadFailed(f"{name}: {message[:300]}") from None
            raise cls(message[:300], **fields)  # http_status and details survive
        return decode(json.loads(raw))["body"]


def _run_status_config() -> Mapping[str, Any]:
    return json.loads(env("AWX_RUN_STATUS"))


def _describe(arn: str) -> Mapping[str, Any] | None:
    try:
        return boto_client("stepfunctions").describe_execution(executionArn=arn)
    except Exception as exc:
        if type(exc).__name__ == "ExecutionDoesNotExist" or (
            getattr(exc, "response", {}).get("Error", {}).get("Code") == "ExecutionDoesNotExist"
        ):
            return None
        raise


def settled_status(
    desc: Mapping[str, Any],
    cfg: Mapping[str, Any],
    action: str,
    now: datetime,
    *,
    child_wait: bool = False,
) -> str:
    """A DescribeExecution answer as run_status reports it (see LambdaEnv.run_status).

    `action` keys `child_timeout_s` (the machine's ARN suffix). An abnormal end (FAILED,
    TIMED_OUT, ABORTED) always waits the child timeout too; `child_wait` makes a SUCCEEDED end
    wait it as well (its children are not proven over)."""
    status = str(desc["status"])
    if status not in RUN_TERMINAL:
        return RUN_RUNNING  # RUNNING, PENDING_REDRIVE, anything new
    stop = desc.get("stopDate")
    if not isinstance(stop, datetime):
        return RUN_RUNNING  # a terminal status with no stop time: cannot tell, so live
    wait = int(cfg["adapter_timeout_s"]) + int(cfg["margin_s"])
    if status != "SUCCEEDED" or child_wait:
        wait += int(cfg["child_timeout_s"].get(action, 0))
    if now - stop < timedelta(seconds=wait):
        return RUN_RUNNING
    return status


def _repo(cls: Any, var: str) -> Any:
    return cls(env(var), client=boto_client("dynamodb"))


def handler(event: dict[str, Any], context: Any) -> Any:
    payload = decode(event)
    phase = Phase(payload["phase"])
    root = root_spec()
    lenv = LambdaEnv(context)

    if phase is Phase.START:
        execution = payload["execution"]
        try:
            tenant = lenv.resolve_tenant(engine.resolve_tenant_id(execution.get("tenant_id")))
            if tenant.kit is None or tenant.kit != root.kit:  # a tenant row is kit-bound
                raise TenantScopeViolation("the run tenant was not resolved in this kit")
            run = engine.start(
                root, execution, tenant=tenant, execution_name=payload["execution_name"]
            )
            # The spec decides whose approvers decide; an undeclared kit 5-8 root fails
            # closed here, before any proposal is stored.
            approver = approver_tenant(
                root, tenant.tenant_id, is_child=run.parent_proposal_id is not None
            )
        except Exception as exc:
            _refused_start(payload, exc, lenv)
            raise
        _repo(ProposalRepo, "AWX_PROPOSALS_TABLE").put(run, approver_tenant=approver)
        log(
            "workflow.start",
            proposal_id=run.proposal.proposal_id,
            kit=root.kit,
            action=run.proposal.action,
            tier=run.proposal.risk_tier.value,
        )
        return engine.to_payload(run)

    run = engine.from_payload(payload["run"])
    pid = run.proposal.proposal_id
    if phase is Phase.REQUEST_APPROVAL:
        _repo(ProposalRepo, "AWX_PROPOSALS_TABLE").set_task_token(
            pid, payload["token"], payload.get("execution_arn")
        )
        log("workflow.awaiting_approval", proposal_id=pid)
        return {"waiting": True}
    if phase is Phase.AUDIT:
        run = engine.abandon(root, run, lenv)
        run, entry = engine.audit(run, now=lenv.now())
        _repo(AuditRepo, "AWX_AUDIT_TABLE").append(entry)
        _repo(ProposalRepo, "AWX_PROPOSALS_TABLE").set_outcome(pid, run.state)
        log("workflow.end", proposal_id=pid, state=run.state)
        return engine.to_payload(run)
    if phase is Phase.POSTCONDITION:
        return engine.to_payload(engine.postcondition_guarded(root, run, lenv))
    if phase is Phase.ERROR:
        err = payload.get("error") or {}
        name = str(err.get("Error", ""))
        return engine.to_payload(
            engine.error(
                run,
                _error_text(err),
                refused=name in engine.REFUSAL_ERRORS,
                reused=name == engine.REUSE_ERROR,
            )
        )
    try:
        run = _phase(phase, root, run, payload, lenv)
    except Exception as exc:
        run = engine.error(run, f"{phase.value} {type(exc).__name__}: {exc}")
    return engine.to_payload(run)


def _phase(
    phase: Phase,
    root: KitWorkflowSpec,
    run: engine.Run,
    payload: Mapping[str, Any],
    lenv: LambdaEnv,
) -> engine.Run:
    if phase is Phase.BINDING:
        decisions = _repo(DecisionRepo, "AWX_DECISIONS_TABLE").list(run.proposal.proposal_id)
        return engine.binding(run, decisions, now=lenv.now())
    if phase is Phase.RECHECK:
        return engine.recheck(root, run, lenv)
    if phase is Phase.FAN_OUT:
        return engine.fan_out(root, run, lenv)
    if phase is Phase.CHILDREN_DONE:
        children = [
            engine.from_payload(c["run"]) if c.get("run") else None for c in payload["children"]
        ]
        run = engine.children_done(run, children)
        if run.lost_children:  # on the proposal row before this run can reach Done
            _repo(ProposalRepo, "AWX_PROPOSALS_TABLE").set_lost_children(
                run.proposal.proposal_id, run.lost_children
            )
        return run
    if phase is Phase.BUILD:
        return engine.build(root, run, payload["step"], lenv)
    if phase is Phase.RESULT:
        return engine.result(root, run, codec.result_from(payload["result"]), lenv)
    if phase is Phase.COMPLETED:
        return engine.completed(root, run, codec.result_from(payload["result"]), lenv)
    if phase is Phase.CRASHED:
        return engine.crashed(root, run, _error_text(payload.get("error") or {}), lenv)
    if phase is Phase.FINALITY_CHECK:
        return engine.finality_check(root, run, lenv)
    raise ValueError(f"unhandled phase {phase.value}")


def _refused_start(payload: Mapping[str, Any], exc: Exception, lenv: LambdaEnv) -> None:
    """A Start refusal leaves one audit row under the execution name."""
    name = str(payload.get("execution_name") or "unknown")
    try:
        _repo(AuditRepo, "AWX_AUDIT_TABLE").append(
            {
                "proposal_id": name,
                "state": "REFUSED",
                "detail": f"start {type(exc).__name__}: {exc}"[:400],
                "at": lenv.now(),
            }
        )
    except Exception as audit_exc:
        log("workflow.start_audit_failed", proposal_id=name, error=type(audit_exc).__name__)
    log("workflow.start_refused", proposal_id=name, error=type(exc).__name__)


def _error_text(err: Mapping[str, Any]) -> str:
    """Step Functions' $.error is {"Error": type, "Cause": Lambda's JSON error envelope}."""
    name = str(err.get("Error", "Error"))
    cause = err.get("Cause", "")
    try:
        message = json.loads(cause).get("errorMessage", "")
    except (TypeError, ValueError, AttributeError):
        message = str(cause)
    return f"{name}: {message}"[:400]
