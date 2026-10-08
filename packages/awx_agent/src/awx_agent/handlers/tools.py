"""Gateway Lambda target for one kit's read_* and propose_* tools.

Identity: the interceptor writes the caller's access token under IDENTITY_FIELD; this handler
verifies it again (signature, issuer, client, token_use, expiry) before doing anything, so a
direct invocation with a forged event gets nothing. Tenant: kits 1-4 serve platform callers only
and run as the platform tenant (any other tenant_id claim, or none, is refused with TENANT_SCOPE);
kits 5-8 run as the caller's tenant_id claim, resolved server-side through the tenants table.
Nothing about identity or tenant comes from model input.

READ: run the kit's READS function with the engine's own pure PolicyContext
(awx_spine_aws.engine.policy_context(..., pure=True)): reads through this kit's reader
(SpineStack.reader_for(kit)), the kit store read-only.

PROPOSE: awx_spine_aws.engine.propose(..., origin=Origin.AGENT) does validate, the pure
policy context, the agent approval floor and the derived proposal ID (which hashes intent and
computed values), which is also the execution name. The kit's KitWorkflow starts with
engine.execution_input. A retry inside one approval window hits ExecutionAlreadyExists and gets
the existing execution's real state; a re-proposal whose computed amounts differ gets a new ID
and a new execution.

A proposal is inert. Its tier is at least SINGLE unless the kit justified agent_min_tier AUTO, so
the workflow waits for an approval from someone other than the proposer. This handler cannot
release that wait: it never sends a task token, never invokes the adapter, never writes the kit
store, and its IAM role allows none of those.

Read results go under `untrusted_data` so the model is reminded that provider content is data.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from awx_common import transport
from awx_common.approvals import Origin, Proposal
from awx_common.errors import AwxError, TenantScopeViolation, error_class
from awx_common.intent import to_jsonable
from awx_common.ops import ReadRequest
from awx_common.store import KitStore
from awx_common.tenancy import TenantContext, TenantResolver, resolve_in_kit
from awx_common.tools import ToolKind, ToolSpec
from awx_common.workflow import KitWorkflowSpec, PolicyContext
from awx_spine_aws import codec, engine

from ..identity import (
    APPROVER_GROUP,
    IDENTITY_FIELD,
    REQUESTER_GROUP,
    CallerIdentity,
    IdentityError,
)
from ..jwtverify import CognitoVerifier
from ..registry import TOOL_TARGET_DELIMITER, load_kit_tools
from ..schema import ToolInputError, check_arguments

log = logging.getLogger(__name__)


class ReaderError(Exception):
    """The reader Lambda failed. The message is safe to show the model."""


class KitValidationError(Exception):
    """The kit's validate() refused the tool input. Its message is safe to show the model."""


@dataclass
class GatewayToolEnv:
    """engine.Env for the tool plane: this kit's own reader, the kit store, server resolver.

    The tool plane only builds pure contexts (engine.policy_context(..., pure=True) and
    engine.propose), so the engine wraps the store read-only (rule 0). Reads go to the per-kit
    reader SpineStack.reader_for(kit) as {kit, read, tenant_id} with no mode: the reader
    derives tenant or platform scope from the request's x-on-behalf-of.
    """

    kit: str
    lambda_client: Any
    reader_fn: str  # SpineStack.reader_for(kit); the only function this env invokes
    resolver: TenantResolver
    store_factory: Callable[[str, TenantContext], KitStore | None]
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    sandbox: bool = True  # Env protocol field; the tool plane never drives simulations
    sleeper: Callable[[float], None] = field(default=time.sleep)
    context: Any = None  # the current invocation's Lambda context (set per call by handler)
    # No run_status here: at propose time a policy sees PolicyContext.run_status = None and
    # decides at recheck, where the kit step supplies it.

    def now(self) -> datetime:
        return self.clock()

    def sleep(self, seconds: float) -> None:
        """engine.retrying's wait between read retries."""
        self.sleeper(seconds)

    def read_budget_s(self) -> float | None:
        """Remaining Lambda time less a whole reader call, as the kit step reserves it."""
        from awx_spine_aws.handlers.kit_step import INVOKE_OVERHEAD_S, READER_TIMEOUT_S

        remaining = getattr(self.context, "get_remaining_time_in_millis", None)
        if remaining is None:
            return None
        return max(remaining() / 1000.0 - READER_TIMEOUT_S - INVOKE_OVERHEAD_S, 0.0)

    def read(self, kit: str, req: ReadRequest, tenant: TenantContext) -> Mapping[str, Any]:
        return self._invoke(kit, req, tenant)  # bound by engine.policy_context (bind_read)

    def read_platform(self, kit: str, req: ReadRequest, tenant: TenantContext) -> Mapping[str, Any]:
        return self._invoke(kit, req, tenant)  # headerless: the reader derives platform scope

    def store(self, kit: str, tenant: TenantContext) -> KitStore | None:
        return self.store_factory(kit, tenant)

    def resolve_tenant(self, tenant_id: str) -> TenantContext:
        """The tool plane resolves tenants bound to its own kit only."""
        return resolve_in_kit(self.resolver, tenant_id, self.kit)

    def _invoke(self, kit: str, req: ReadRequest, tenant: TenantContext) -> Mapping[str, Any]:
        if kit != self.kit:
            raise TenantScopeViolation(f"the {self.kit} tool plane may not read for {kit}")
        event = {"kit": kit, "read": req, "tenant_id": tenant.tenant_id}
        resp = self.lambda_client.invoke(
            FunctionName=self.reader_fn,
            InvocationType="RequestResponse",
            Payload=transport.dumps(event).encode("utf-8"),
        )
        raw = resp["Payload"].read()
        if resp.get("FunctionError"):
            try:
                err = json.loads(raw or b"{}")
                name = str(err.get("errorType", ""))
                cls = error_class(name)
                _, fields = codec.error_fields(str(err.get("errorMessage", "")))
            except (KeyError, ValueError, AttributeError):
                raise ReaderError(
                    f"the reader refused or failed GET {event['read'].path}"
                ) from None
            # http_status and details (Retry-After) survive for engine.retrying
            raise cls(f"the reader refused GET {event['read'].path}", **fields)
        body = transport.loads(raw.decode("utf-8"))
        if not isinstance(body, Mapping) or "body" not in body:
            raise ReaderError("reader returned an unexpected payload")
        return body["body"]


@dataclass(frozen=True)
class _KitReadScope:
    """Duck-typed root spec for read_* tools: the union of the kit's allowlists, so READS get
    exactly the PolicyContext engine.policy_context builds for the kit's workflows."""

    kit: str
    read_allowlist: tuple[str, ...]
    platform_read_allowlist: tuple[str, ...]
    child_tenant: Any = None
    child: Any = None


def _read_scope(kit: str, specs: Mapping[str, KitWorkflowSpec]) -> _KitReadScope:
    tenant: list[str] = []
    platform: list[str] = []
    has_child_tenant = False
    for root in specs.values():
        tenant.extend(p for p in engine.allowlist(root) if p not in tenant)
        platform.extend(p for p in engine.platform_allowlist(root) if p not in platform)
        has_child_tenant = has_child_tenant or engine._has_child_tenant(root)
    return _KitReadScope(
        kit=kit,
        read_allowlist=tuple(tenant),
        platform_read_allowlist=tuple(platform),
        child_tenant=(lambda _intent: "") if has_child_tenant else None,
    )


@dataclass
class ToolDispatcher:
    """Everything a tool call needs, injected so tests can stub AWS."""

    kit: str
    tools: Mapping[str, ToolSpec]
    specs: Mapping[str, KitWorkflowSpec]
    reads: Mapping[str, Callable[[Mapping[str, Any], PolicyContext], Mapping[str, Any]]]
    env: Any  # engine.Env
    sfn_client: Any
    state_machines: Mapping[str, str]
    verifier: CognitoVerifier
    # proposal_id -> recorded outcome (REJECTED, EXPIRED, SUCCEEDED ...) or None while open
    proposal_outcome: Callable[[str], str | None] | None = None

    # --- entry -----------------------------------------------------------------------------
    def dispatch(self, tool_name: str, event: Mapping[str, Any]) -> dict[str, Any]:
        name = tool_name.split(TOOL_TARGET_DELIMITER, 1)[-1]
        spec = self.tools.get(name)
        if spec is None:
            return _error("UNKNOWN_TOOL", f"{name!r} is not a tool of kit {self.kit}")
        try:
            token = event.get(IDENTITY_FIELD)
            if not isinstance(token, str) or not token:
                raise IdentityError("tool call carries no caller token")
            caller = self.verifier.verify(token)
            if spec.kind is ToolKind.READ:
                caller.require_group(REQUESTER_GROUP, APPROVER_GROUP)
            else:
                caller.require_group(REQUESTER_GROUP)
            # Checked before the tool looks at its arguments, so a caller of another kit learns
            # nothing about this kit's tools. Kits 1-4 accept only a caller whose claim is
            # exactly "platform" (run_tenant raises TenantScopeViolation for anything else, with
            # or without a tenants-table row); kits 5-8 resolve the claim's row in this kit,
            # which refuses an unknown tenant and a row bound to another kit.
            run_as = caller.run_tenant(self.kit)
            tenant = self.env.resolve_tenant(run_as)
            args = check_arguments(spec, event)
            if spec.kind is ToolKind.READ:
                return self._read(spec, args, tenant)
            return self._propose(spec, args, caller, tenant)
        except IdentityError as exc:
            return _error("NOT_AUTHORISED", str(exc))
        except TenantScopeViolation as exc:
            return _error("TENANT_SCOPE", exc.args[0] if exc.args else "tenant scope violation")
        except ToolInputError as exc:
            return _error("INVALID_INPUT", str(exc))
        except ReaderError as exc:
            return _error("READ_FAILED", str(exc))
        except AwxError as exc:
            return _error(type(exc).__name__, exc.args[0] if exc.args else type(exc).__name__)
        except engine.PurityViolation:
            log.error("kit %s wrote the store from the tool plane (rule 0)", self.kit)
            return _error("INTERNAL", "kit code tried to write state while previewing")
        except KitValidationError as exc:
            # Raised only by the kit's own validate(); its message is kit-authored and safe.
            return _error("INVALID_INPUT", str(exc))
        except Exception as exc:  # anything else is a bug; never leak its detail to the model
            log.error("tool %s failed: %s", name, type(exc).__name__)
            return _error("INTERNAL", "the tool failed; nothing was proposed")

    # --- tools -----------------------------------------------------------------------------
    def _read(
        self, spec: ToolSpec, args: Mapping[str, Any], tenant: TenantContext
    ) -> dict[str, Any]:
        scope = _read_scope(self.kit, self.specs)
        ctx = engine.policy_context(scope, self.env, tenant, pure=True)  # type: ignore[arg-type]
        result = self.reads[spec.name](args, ctx)
        return {"tool": spec.name, "untrusted_data": to_jsonable(result)}

    def _propose(
        self,
        spec: ToolSpec,
        args: Mapping[str, Any],
        caller: CallerIdentity,
        tenant: TenantContext,
    ) -> dict[str, Any]:
        action = spec.name.removeprefix("propose_")
        root = self.specs[action]
        arn = self.state_machines.get(action)
        if not arn:
            raise ToolInputError(f"{spec.name} has no deployed workflow")
        try:
            root.validate(args)  # pure (rule 0); engine.propose runs it again
        except (ValueError, TypeError, ArithmeticError) as exc:
            raise KitValidationError(str(exc)[:300] or type(exc).__name__) from None
        # engine.propose: pure policy context, the agent approval floor and the derived
        # proposal ID (also the execution name). This handler creates no proposal and no ID itself.
        proposal, decision = engine.propose(
            root,
            (),
            args,
            proposed_by=caller.sub,
            tenant=tenant,
            env=self.env,
            origin=Origin.AGENT,
        )
        execution = engine.execution_input(proposal, decision, args, tenant.tenant_id)
        try:
            self.sfn_client.start_execution(
                stateMachineArn=arn,
                name=proposal.proposal_id,
                input=transport.dumps(execution),
            )
        except Exception as exc:
            if _error_code(exc) != "ExecutionAlreadyExists":
                raise
            return self._existing(spec.name, arn, proposal.proposal_id, caller)
        return _summary(spec.name, self.kit, proposal, retry=False)

    def _existing(
        self, tool: str, arn: str, proposal_id: str, caller: CallerIdentity
    ) -> dict[str, Any]:
        """A retry reached an execution that already exists: report what really happened to
        it (execution status and the proposal's recorded outcome), never a tier-derived one."""
        desc = self.sfn_client.describe_execution(executionArn=execution_arn(arn, proposal_id))
        execution = transport.loads(desc["input"])
        proposal = proposal_from(execution["proposal"])
        if proposal.proposal_id != proposal_id or proposal.proposed_by != caller.sub:
            raise IdentityError("an execution with this proposal ID belongs to someone else")
        execution_status = str(desc.get("status", "UNKNOWN"))
        outcome = self.proposal_outcome(proposal_id) if self.proposal_outcome else None
        if outcome:
            state = outcome
        elif execution_status == "RUNNING":
            state = "IN_PROGRESS"  # an AUTO run is executing; any other may await approval
        else:
            state = f"EXECUTION_{execution_status}"
        out = _summary(tool, self.kit, proposal, retry=True)
        out["status"] = f"EXISTING_PROPOSAL_{state}"
        out["status_message"] = _existing_message(state, proposal.risk_tier.value)
        out["execution_status"] = execution_status
        out["proposal_state"] = state
        return out


def proposal_from(data: Mapping[str, Any]) -> Proposal:
    """Decode a transported Proposal with the awx_spine_aws codec and check its hash."""
    proposal = data if isinstance(data, Proposal) else codec.proposal_from(data)
    proposal.verify_hash()
    return proposal


def execution_arn(state_machine_arn: str, name: str) -> str:
    prefix, sep, machine = state_machine_arn.rpartition(":stateMachine:")
    if not sep:
        raise ValueError("not a state machine ARN")
    return f"{prefix}:execution:{machine}:{name}"


def _summary(tool: str, kit: str, proposal: Proposal, *, retry: bool) -> dict[str, Any]:
    return {
        "tool": tool,
        "proposal_id": proposal.proposal_id,
        "intent_hash": proposal.intent_hash,
        "risk_tier": proposal.risk_tier.value,
        "origin": proposal.origin.value,
        "reasons": list(proposal.reasons),
        "status": _status_for(proposal.risk_tier.value),
        "status_message": _status_message(proposal.risk_tier.value),
        "existing_proposal": retry,
        "expires_at": to_jsonable(proposal.expires_at),
        "summary": {
            "kit": kit,
            "action": proposal.action,
            "intent": to_jsonable(proposal.intent),
            "computed_by_code": to_jsonable(proposal.computed),
        },
    }


def _status_for(tier: str) -> str:
    return {
        "AUTO": "SUBMITTED_NO_APPROVAL_NEEDED",
        "BLOCKED": "BLOCKED_BY_POLICY",
    }.get(tier, "AWAITING_APPROVAL")


# What each status means, in words the model can repeat to the user. An AUTO proposal's run
# starts without approval when the tool returns, but the workflow rechecks policy before its
# first step, so the message claims neither that it waits nor that money has moved.
AUTO_RECHECK = (
    "The workflow rechecks policy before its first step and may still stop it. This result "
    "does not say whether money has moved."
)
STATUS_MESSAGES = {
    "AUTO": f"The run has started without approval. {AUTO_RECHECK}",
    "BLOCKED": "Policy blocked this proposal. Nothing has run and nothing will run.",
}
AWAITING_MESSAGE = (
    "Nothing has run yet. The proposal waits for approval by someone other than the proposer."
)


def _status_message(tier: str) -> str:
    return STATUS_MESSAGES.get(tier, AWAITING_MESSAGE)


def _existing_message(state: str, tier: str) -> str:
    if state == "IN_PROGRESS" and tier == "AUTO":
        return (
            "This proposal already exists and its run is executing; it needs no approval. "
            + AUTO_RECHECK
        )
    if state == "IN_PROGRESS":
        return (
            "This proposal already exists and its run is in progress: it is waiting for "
            "approval or executing, and this result does not say which."
        )
    return f"This proposal already exists. Its recorded state is {state}."


def _error(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


def _error_code(exc: BaseException) -> str | None:
    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        return (response.get("Error") or {}).get("Code")
    return None


# --- Lambda wiring -----------------------------------------------------------------------------
_DISPATCHER: ToolDispatcher | None = None


def _build_from_env() -> ToolDispatcher:
    import boto3
    from awx_spine_aws.kit_store import DynamoDbKitStore
    from awx_spine_aws.repos import DynamoDbTenantResolver

    kit = os.environ["AWX_KIT"]
    plan = load_kit_tools(kit, os.environ["AWX_TOOLS_MODULE"])
    ddb = boto3.client("dynamodb")
    ledger = os.environ["AWX_LEDGER_TABLE"]
    env = GatewayToolEnv(
        kit=kit,
        lambda_client=boto3.client("lambda"),
        reader_fn=os.environ["AWX_READER_FN"],
        resolver=DynamoDbTenantResolver(os.environ["AWX_TENANTS_TABLE"], client=ddb),
        store_factory=lambda k, t: DynamoDbKitStore(
            ledger, kit=k, tenant_id=t.tenant_id, client=ddb
        ),
    )
    proposals = os.environ["AWX_PROPOSALS_TABLE"]

    def proposal_outcome(proposal_id: str) -> str | None:
        # Only these two attributes: the IAM policy allows no others (task_token stays unread).
        item = ddb.get_item(
            TableName=proposals,
            Key={"proposal_id": {"S": proposal_id}},
            ProjectionExpression="proposal_id, outcome",
            ConsistentRead=True,
        ).get("Item")
        return (item or {}).get("outcome", {}).get("S")

    return ToolDispatcher(
        kit=kit,
        tools={t.name: t for t in plan.tools},
        specs=plan.specs,
        reads=plan.reads,
        env=env,
        sfn_client=boto3.client("stepfunctions"),
        state_machines=json.loads(os.environ["AWX_STATE_MACHINES"]),
        verifier=CognitoVerifier.for_issuer(
            os.environ["AWX_EXPECTED_ISSUER"], os.environ["AWX_EXPECTED_CLIENT_ID"]
        ),
        proposal_outcome=proposal_outcome,
    )


def tool_name_from_context(context: Any) -> str:
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    name = custom.get("bedrockAgentCoreToolName")
    if not name:
        raise IdentityError("not invoked by AgentCore Gateway")
    return str(name)


def handler(event: Mapping[str, Any], context: Any) -> dict[str, Any]:
    global _DISPATCHER
    if _DISPATCHER is None:
        _DISPATCHER = _build_from_env()
    try:
        name = tool_name_from_context(context)
    except IdentityError as exc:
        return _error("NOT_AUTHORISED", str(exc))
    _DISPATCHER.env.context = context  # read retries stay inside this invocation's time
    return _DISPATCHER.dispatch(name, event)
