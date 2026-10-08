"""KitWorkflow: one kit action's Step Functions Standard state machine, built from its spec.

The construct reads the kit's WorkflowManifest (infra.kit_manifest; kit code never runs in the
CDK process) and lays the states out from it,
so step order, finality loops, the approval timeout and fan_out come from the spec and from
nowhere else. Each state calls awx_spine_aws.engine through the kit-step Lambda (bundled with
the kit's package) or the spine's adapter Lambda; local_runner calls the same phases in the
same order, and awx_spine_aws/tests/test_asl_equivalence.py interprets this construct's
synthesised ASL and checks both runners agree.

    Start -> RiskTier? --AUTO--> Recheck -> [FanOut -> Children (Map: one child execution
               |                 each, named after its proposal) -> ChildrenDone]
               |                 -> per step: Build -> Route? --pending--> Dispatch?
               |                       send/advance -> Adapter -> Result -> Route?
               |                       resend -> ResendWait -> Adapter
               |                       complete -> AdapterComplete -> Completed -> Route?
               |                       Adapter failure after a possible send -> Crashed -> Route?
               |                    Route? --finality--> FinalityWait -> FinalityCheck -> Route?
               |                 -> AfterStop -> Postcondition -> Audit -> Done
               +--BLOCKED--> Audit
               +--SINGLE/DUAL (root only)--> RequestApproval (waitForTaskToken, timeout
                                 approval_ttl) -> Binding -> Approved? -> Recheck ...

The Adapter task never retries a send: an adapter timeout, crash or any error other than a
refusal (NotAuthorised, TenantScopeViolation, RequestIdReuse, which go to Error with nothing
sent) goes to Crashed, which reconciles before the engine decides on a resend (rule 9). Only a
throttle on the invoke itself (the Lambda never ran) is retried. Postcondition and Audit have
no automatic retry, so they run once. A child machine has no approval states at all. The state
machine role is the ONLY principal granted lambda:InvokeFunction on the adapter.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from awx_common.approvals import RiskTier
from awx_spine_aws import engine
from awx_spine_aws.engine import Phase
from cdk_nag import NagPackSuppression, NagSuppressions
from constructs import Construct

from infra.constructs.spine_lambda import (
    LOG_RETENTION,
    SPINE_PACKAGES,
    kms_use,
    lambda_role,
    spine_function,
    staged_code,
    table_access,
)
from infra.kit_manifest import WorkflowManifest

STEP_HANDLER = "awx_spine_aws.handlers.kit_step.handler"
_ID = re.compile(r"[^A-Za-z0-9]")


def _camel(text: str) -> str:
    return "".join(part.capitalize() for part in _ID.split(text) if part)


MACHINE_PREFIX = "awx-"
RUN_STATUS_MARGIN_S = 60  # past the adapter timeout before a stopped run reads terminal
_MACHINE_NAME = re.compile(r"[A-Za-z0-9_-]{1,80}")


def root_machine_name(kit: str, action: str) -> str:
    """The root state machine's name, awx-<kit>-<action>. Unique for this PoC's one
    deployment per account and region, and a valid Step Functions name (<= 80 characters)."""
    name = f"{MACHINE_PREFIX}{kit}-{action}"
    if not _MACHINE_NAME.fullmatch(name) or "-" in kit:
        raise ValueError(f"{name!r} is not a usable state machine name")
    return name


def _child_timeouts(workflows: Mapping[str, WorkflowManifest]) -> dict[str, int]:
    out: dict[str, int] = {}
    for action, spec in sorted(workflows.items()):
        name, node = action, spec
        while node is not None:
            child = node.child
            out[name] = int(run_timeout(child, child=True).to_seconds()) if child else 0
            name, node = f"{name}-child", child
    return out


def run_timeout(spec: WorkflowManifest, *, child: bool = False) -> Duration:
    """Upper bound for one execution: the approval wait (root only, a child never waits),
    every step's finality loop and resend waits, ceil(MAX_FAN_OUT / concurrency) rounds of
    children, and an hour of slack."""
    seconds = 0 if child else int(spec.approval_ttl.total_seconds())
    return Duration.seconds(seconds + engine.child_budget_s(spec) + 3600)


class KitWorkflow(Construct):
    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        spine,  # SpineStack; untyped to avoid importing the stack module here
        kit: str,
        action: str,
        spec_module: str,
        workflow: WorkflowManifest,
        sandbox: bool | None = None,
        allow_stand_in: bool = False,  # tests with an in-memory spec module only
        kit_workflows: Mapping[str, WorkflowManifest] | None = None,
    ) -> None:
        """`workflow` is the action's manifest (infra.kit_manifest), never the kit's spec
        object: the CDK process does not import kit code. `spec_module` is only the module
        name the step Lambda imports at run time."""
        super().__init__(scope, id)
        if sandbox is None:  # rule 10; this PoC deploys sandbox only
            sandbox = str(self.node.try_get_context("sandbox") or "true").lower() != "false"
        self.sandbox = sandbox
        spec = workflow
        if not isinstance(spec, WorkflowManifest):
            raise TypeError("KitWorkflow takes a WorkflowManifest (infra.kit_manifest)")
        if spec.kit != kit or spec.action != action:
            raise ValueError(f"manifest for {spec_module}[{action!r}] is {spec.kit}.{spec.action}")
        self.spec = spec
        self.spine = spine
        # Every action of the kit (KitStack passes manifest.workflows), for run_status.
        kit_workflows = dict(kit_workflows or {action: workflow})
        if kit_workflows.get(action) is not workflow:
            raise ValueError("kit_workflows must hold this workflow under its action")
        spine.register_read_allowlist(kit, engine.allowlist(spec), engine.platform_allowlist(spec))
        self.reader_fn = spine.reader_for(kit)  # this kit's reader only

        top = spec_module.split(".", 1)[0]
        code = staged_code(f"{kit}-{action}", (*SPINE_PACKAGES, top), allow_stand_in=allow_stand_in)
        stack = Stack.of(self)
        # Every root machine is named per (kit, action), so any construct (and the step
        # Lambda of any of the kit's actions) can build its execution ARNs without a reference.
        self.root_machine_name = root_machine_name(kit, action)
        self.kit_execution_prefix = (
            f"arn:{stack.partition}:states:{stack.region}:{stack.account}:execution:"
            f"{MACHINE_PREFIX}{kit}-"
        )
        self.root_execution_prefix = f"{self.kit_execution_prefix}{action}:"
        from infra.stacks.spine_stack import ADAPTER_TIMEOUT_S

        self.run_status_config = {
            "prefix": self.kit_execution_prefix,  # + action + ":" + proposal ID
            "actions": sorted(kit_workflows),
            "adapter_timeout_s": ADAPTER_TIMEOUT_S,
            "margin_s": RUN_STATUS_MARGIN_S,
            # keyed by machine ARN suffix: <action> for a root, <action>-child for its child...
            "child_timeout_s": _child_timeouts(kit_workflows),
        }
        self.step_role = self._step_role()
        self.step_fn = spine_function(
            self,
            "StepFn",
            handler=STEP_HANDLER,
            code=code,
            role=self.step_role,
            key=spine.kms_key,
            timeout=Duration.seconds(60),
            description=f"{kit} {action}: deterministic workflow phases (no Airwallex secret)",
            environment={
                **spine.spine_environment(),
                "AWX_SPEC_MODULE": spec_module,
                "AWX_ACTION": action,
                "AWX_READER_FUNCTION": self.reader_fn.function_name,
                "AWX_SANDBOX_MODE": "1" if sandbox else "0",
                # run_status describes this kit's root executions. Root machine names are
                # fixed, so this is plain data, not a reference back to any machine.
                "AWX_RUN_STATUS": json.dumps(self.run_status_config, sort_keys=True),
            },
        )
        self.child_workflows: list[sfn.StateMachine] = []
        self.state_machine = self._machine(spec, ())
        # PolicyContext.run_status: DescribeExecution on this kit's root executions only
        # (every action's), never another kit's; a separate policy, so no cycle.
        step_describe = iam.Policy(
            self,
            "StepRunStatus",
            roles=[self.step_role],
            statements=[
                iam.PolicyStatement(
                    actions=["states:DescribeExecution"],
                    resources=[f"{self.kit_execution_prefix}*:*"],
                )
            ],
        )
        NagSuppressions.add_resource_suppressions(
            step_describe,
            [
                NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason=(
                        "Execution ARNs end in the execution name, a proposal ID known only at "
                        "run time, and a kit's actions are its machines; the grant is "
                        "DescribeExecution on this kit's root machines (awx-<kit>-*) only."
                    ),
                    applies_to=[{"regex": "/^Resource::arn:<AWS::Partition>:states:.*:\\*$/"}],
                )
            ],
        )
        # The approval API checks a run is still waiting before it reports a decision whose
        # token send failed: DescribeExecution on this kit's root executions only.
        # The policy sits on the spine's ONE approval role, and an inline policy name is unique
        # per role. CDK derives it from the stack-local logical id, which two kits with the
        # same action name share (k2 and k5 both have issue_card), so name it per kit+action.
        describe = iam.Policy(
            self,
            "ApprovalDescribe",
            policy_name=f"awx-{kit}-{action}-approval-describe",
            roles=[spine.approval_role],
            statements=[
                iam.PolicyStatement(
                    actions=["states:DescribeExecution"],
                    resources=[
                        f"arn:{Stack.of(self).partition}:states:{Stack.of(self).region}:"
                        f"{Stack.of(self).account}:execution:"
                        f"{self.state_machine.state_machine_name}:*"
                    ],
                )
            ],
        )
        NagSuppressions.add_resource_suppressions(
            describe,
            [
                NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason=(
                        "Execution ARNs end in the execution name, a proposal ID known only at "
                        "run time; the grant is DescribeExecution on this kit's root machine."
                    ),
                    applies_to=[{"regex": "/^Resource::arn:<AWS::Partition>:states:.*:\\*$/"}],
                )
            ],
        )

    # --- roles ---------------------------------------------------------------------------

    def _step_role(self) -> iam.Role:
        s = self.spine
        role = lambda_role(self, "StepRole", description="Kit workflow phases; no secret")
        role.add_to_policy(kms_use(s.kms_key))
        role.add_to_policy(
            table_access(s.proposals_table.table_arn, ["GetItem", "PutItem", "UpdateItem"])
        )
        role.add_to_policy(table_access(s.decisions_table.table_arn, ["Query"]))
        role.add_to_policy(table_access(s.tenants_table.table_arn, ["GetItem"]))
        role.add_to_policy(table_access(s.audit_table.table_arn, ["PutItem"]))
        # The ledger is partitioned "<kit>#<tenant>", so IAM pins each kit to its own
        # partitions. The proposals, decisions and audit tables cannot be
        # prefixed the same way: their partition key is the proposal ID, a UUID derived by
        # approvals.derive_proposal_id that names no kit (it is also the execution name, which
        # the agent plane's propose tool must be able to compute). Their kit isolation rests on the
        # adapter, which re-reads each proposal and checks its kit and intent hash.
        ledger = table_access(
            s.ledger_table.table_arn, ["GetItem", "PutItem", "UpdateItem", "Query"]
        )
        ledger.add_condition(
            "ForAllValues:StringLike", {"dynamodb:LeadingKeys": [f"{self.spec.kit}#*"]}
        )
        role.add_to_policy(ledger)
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"], resources=[self.reader_fn.function_arn]
            )
        )
        return role

    # --- state machine -------------------------------------------------------------------

    def _machine(self, spec: WorkflowManifest, path: Sequence[str]) -> sfn.StateMachine:
        child = self._machine(spec.child, (*path, "child")) if spec.child is not None else None
        if child is not None:
            self.child_workflows.append(child)
        prefix = "".join(_camel(p) for p in path)
        name = prefix or "Root"
        definition = _Definition(
            self, tuple(path), self.step_fn, self.spine.adapter_fn, spec, child
        )
        log_group = logs.LogGroup(
            self,
            f"{name}MachineLogs",
            retention=LOG_RETENTION,
            encryption_key=self.spine.kms_key,
            removal_policy=RemovalPolicy.DESTROY,
        )
        machine = sfn.StateMachine(
            self,
            f"{name}Machine",
            # For run_status every machine has a fixed name; a child is awx-<kit>-<action>-child
            state_machine_name="-".join((self.root_machine_name, *path)),
            definition_body=sfn.DefinitionBody.from_chainable(definition.start),
            state_machine_type=sfn.StateMachineType.STANDARD,
            timeout=run_timeout(spec, child=bool(path)),
            tracing_enabled=True,
            logs=sfn.LogOptions(
                destination=log_group, level=sfn.LogLevel.ALL, include_execution_data=False
            ),
            comment=f"{spec.kit} {spec.action}{' child' if path else ''}: Airwallex PoC workflow",
        )
        NagSuppressions.add_resource_suppressions(
            machine,
            [
                NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason=(
                        "CDK-generated: Lambda invoke grants cover function versions (:*); "
                        "CloudWatch Logs delivery and X-Ray APIs have no resource-level "
                        "permissions; child execution grants cover that machine's executions."
                    ),
                    applies_to=[
                        "Resource::*",
                        {"regex": "/^Resource::<.*Fn.*\\.Arn>:\\*$/"},
                        {"regex": "/^Resource::arn:<AWS::Partition>:states:.*$/"},
                        {"regex": "/^Resource::.*AwxSpine.*Fn.*$/"},
                    ],
                )
            ],
            apply_to_children=True,
        )
        action = cw_actions.SnsAction(self.spine.alarm_topic)
        for metric_name, metric in (
            ("Failed", machine.metric_failed(period=Duration.minutes(5))),
            ("TimedOut", machine.metric_timed_out(period=Duration.minutes(5))),
        ):
            cw.Alarm(
                self,
                f"{name}{metric_name}Alarm",
                alarm_description=f"{spec.kit} {spec.action} executions {metric_name.lower()}",
                metric=metric,
                threshold=1,
                evaluation_periods=1,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            ).add_alarm_action(action)
        return machine


REFUSALS = ["NotAuthorised", "TenantScopeViolation", "RequestIdReuse"]  # nothing was sent
assert set(REFUSALS) == engine.REFUSAL_ERRORS | {engine.REUSE_ERROR}  # all routed to Error


class _Definition:
    """Builds the states for one (sub)spec inside the KitWorkflow construct."""

    def __init__(
        self,
        scope: Construct,
        path: tuple[str, ...],
        step_fn: lambda_.IFunction,
        adapter_fn: lambda_.IFunction,
        spec: WorkflowManifest,
        child: sfn.StateMachine | None,
    ) -> None:
        self.scope = scope
        self.path = path
        self.prefix = "".join(_camel(p) for p in path)
        self.step_fn = step_fn
        self.adapter_fn = adapter_fn

        # Audit runs on_abandon (when no step executed) and writes the audit record. It and the
        # postcondition get no automatic retry, so neither runs twice.
        self.audit = self._task("Audit", Phase.AUDIT, retry=False)
        self.audit.next(sfn.Succeed(scope, self._id("Done"), state_name="Done"))
        self.final_error = self._task(
            "PostconditionError", Phase.ERROR, retry=False, error=sfn.JsonPath.object_at("$.error")
        )
        self.final_error.next(self.audit)
        self.postcondition = self._task("Postcondition", Phase.POSTCONDITION, retry=False)
        self.postcondition.add_catch(self.final_error, errors=["States.ALL"], result_path="$.error")
        self.postcondition.next(self.audit)
        # Rule 2: once steps have started, any stop goes through the postcondition first.
        self.after_stop = sfn.Choice(scope, self._id("AfterStop"), state_name="AfterStop")
        self.after_stop.when(
            sfn.Condition.boolean_equals("$.run.postcondition_due", True), self.postcondition
        )
        self.after_stop.otherwise(self.audit)
        self.error = self._task("Error", Phase.ERROR, error=sfn.JsonPath.object_at("$.error"))
        self.error.next(self.after_stop)

        self.start = self._task(
            "Start",
            Phase.START,
            run=False,
            execution=sfn.JsonPath.object_at("$$.Execution.Input"),
            execution_name=sfn.JsonPath.string_at("$$.Execution.Name"),
        )
        recheck = self._task("Recheck", Phase.RECHECK, catch=True)
        rechecked = self._stopped_or("Rechecked")
        rechecked.otherwise(self._executed(spec, child))
        recheck.next(rechecked)

        tier = sfn.Choice(scope, self._id("RiskTier"), state_name="RiskTier")
        tier.when(sfn.Condition.is_string("$.run.state"), self.audit)
        tier.when(
            sfn.Condition.string_equals("$.run.proposal.risk_tier", RiskTier.AUTO.value), recheck
        )
        if path:  # rule 3: a child never waits for approval; start already failed it closed
            tier.otherwise(self.audit)
        else:
            tier.when(
                sfn.Condition.string_equals("$.run.proposal.risk_tier", RiskTier.BLOCKED.value),
                self.audit,
            )
            tier.otherwise(self._approval(spec, recheck))
        self.start.next(tier)

    def _approval(self, spec: WorkflowManifest, recheck: sfn.IChainable) -> sfn.IChainable:
        request = tasks.LambdaInvoke(
            self.scope,
            self._id("RequestApproval"),
            state_name="RequestApproval",
            lambda_function=self.step_fn,
            integration_pattern=sfn.IntegrationPattern.WAIT_FOR_TASK_TOKEN,
            payload=sfn.TaskInput.from_object(
                {
                    "phase": Phase.REQUEST_APPROVAL.value,
                    "run": sfn.JsonPath.object_at("$.run"),
                    "token": sfn.JsonPath.task_token,
                    "execution_arn": sfn.JsonPath.string_at("$$.Execution.Id"),
                }
            ),
            task_timeout=sfn.Timeout.duration(
                Duration.seconds(int(spec.approval_ttl.total_seconds()))
            ),
            result_path="$.signal",
        )
        # Whether a decision arrived or the wait timed out, Binding recomputes the approval
        # from the stored decisions; still PENDING then means EXPIRED.
        binding = self._task("Binding", Phase.BINDING, catch=True)
        request.add_catch(binding, errors=["States.Timeout"], result_path="$.error")
        request.add_catch(self.error, errors=["States.ALL"], result_path="$.error")
        approved = sfn.Choice(self.scope, self._id("Approved"), state_name="Approved")
        approved.when(sfn.Condition.string_equals("$.run.approval", "APPROVED"), recheck)
        approved.otherwise(self.audit)
        request.next(binding)
        binding.next(approved)
        return request

    def _id(self, name: str) -> str:
        return f"{self.prefix}{name}"

    def _task(
        self,
        name: str,
        phase: Phase,
        *,
        run: bool = True,
        catch: bool = False,
        retry: bool = True,
        **extra,
    ) -> tasks.LambdaInvoke:
        payload = {"phase": phase.value, **extra}
        if run:
            payload["run"] = sfn.JsonPath.object_at("$.run")
        task = tasks.LambdaInvoke(
            self.scope,
            self._id(name),
            state_name=name,
            lambda_function=self.step_fn,
            payload=sfn.TaskInput.from_object(payload),
            payload_response_only=True,
            result_path="$.run",
            retry_on_service_exceptions=retry,
        )
        if catch:
            task.add_catch(self.error, errors=["States.ALL"], result_path="$.error")
        return task

    def _stopped_or(self, name: str) -> sfn.Choice:
        """A Choice whose first rule sends a run that already has an outcome to AfterStop."""
        choice = sfn.Choice(self.scope, self._id(name), state_name=name)
        choice.when(sfn.Condition.is_string("$.run.state"), self.after_stop)
        return choice

    def _executed(self, spec: WorkflowManifest, child: sfn.StateMachine | None) -> sfn.IChainable:
        """Everything after the recheck. Built back to front so each state can point at the
        one after it."""
        tail: sfn.IChainable = self.after_stop
        for index in reversed(range(len(spec.steps))):
            tail = self._step(index, spec.steps[index], tail)
        if child is None:
            return tail
        fan = self._task("FanOut", Phase.FAN_OUT, catch=True)
        fanned = self._stopped_or("FannedOut")
        start_child = tasks.StepFunctionsStartExecution(
            self.scope,
            self._id("StartChild"),
            state_name="StartChild",
            state_machine=child,
            integration_pattern=sfn.IntegrationPattern.RUN_JOB,
            input=sfn.TaskInput.from_object(
                {
                    key: sfn.JsonPath.object_at(f"$.{key}")
                    for key in ("proposal", "raw_input", "decision", "spec_path")
                }
                | {
                    "tenant_id": sfn.JsonPath.string_at("$.tenant_id"),
                    "origin": sfn.JsonPath.string_at("$.origin"),
                    "parent_proposal_id": sfn.JsonPath.string_at("$.parent_proposal_id"),
                    "root_proposal_id": sfn.JsonPath.string_at("$.root_proposal_id"),
                    "root_action": sfn.JsonPath.string_at("$.root_action"),
                }
            ),
            name=sfn.JsonPath.string_at("$.proposal.proposal_id"),
            associate_with_parent=True,
            result_selector={"run": sfn.JsonPath.object_at("$.Output.run")},
        )
        # A child that fails as an execution (not as a run) becomes {"run": null}; every other
        # child still finishes its own postcondition and audit.
        child_lost = sfn.Pass(
            self.scope,
            self._id("ChildLost"),
            state_name="ChildLost",
            parameters={"run": None, "error.$": "$.Error"},
        )
        start_child.add_catch(child_lost, errors=["States.ALL"])
        children = sfn.Map(
            self.scope,
            self._id("Children"),
            state_name="Children",
            items_path="$.run.children",
            max_concurrency=engine.MAP_CONCURRENCY,
            result_path="$.children",
        )
        children.item_processor(start_child)
        children.add_catch(self.error, errors=["States.ALL"], result_path="$.error")
        done = self._task(
            "ChildrenDone",
            Phase.CHILDREN_DONE,
            catch=True,
            children=sfn.JsonPath.object_at("$.children"),
        )
        after = self._stopped_or("ChildrenOk")
        after.otherwise(tail)
        fan.next(fanned)
        fanned.otherwise(children)
        children.next(done)
        done.next(after)
        return fan

    def _step(self, index: int, step, after: sfn.IChainable) -> sfn.IChainable:
        n = f"{index + 1}{_camel(step.name)}"
        build = self._task(f"Build{n}", Phase.BUILD, catch=True, step=step.name)
        route = self._stopped_or(f"Route{n}")
        dispatch = sfn.Choice(self.scope, self._id(f"Dispatch{n}"), state_name=f"Dispatch{n}")

        adapter = tasks.LambdaInvoke(
            self.scope,
            self._id(f"Adapter{n}"),
            state_name=f"Adapter{n}",
            lambda_function=self.adapter_fn,
            payload=sfn.TaskInput.from_object(
                {
                    "mode": sfn.JsonPath.string_at("$.run.step.pending.action"),
                    "operation": sfn.JsonPath.object_at("$.run.step.pending.op"),
                    "proposal_id": sfn.JsonPath.string_at("$.run.proposal.proposal_id"),
                }
            ),
            payload_response_only=True,
            result_path="$.adapter",
            retry_on_service_exceptions=False,  # never resend without a reconcile (rule 9)
        )
        # The Lambda never ran, so nothing was sent: the only safe automatic retry.
        adapter.add_retry(
            errors=["Lambda.TooManyRequestsException"],
            interval=Duration.seconds(2),
            max_attempts=4,
            backoff_rate=2,
        )
        crashed = self._task(
            f"Crashed{n}", Phase.CRASHED, catch=True, error=sfn.JsonPath.object_at("$.error")
        )
        adapter.add_catch(self.error, errors=REFUSALS, result_path="$.error")
        adapter.add_catch(crashed, errors=["States.ALL"], result_path="$.error")
        result = self._task(
            f"Result{n}",
            Phase.RESULT,
            catch=True,
            result=sfn.JsonPath.object_at("$.adapter.result"),
        )
        complete = tasks.LambdaInvoke(
            self.scope,
            self._id(f"AdapterComplete{n}"),
            state_name=f"AdapterComplete{n}",
            lambda_function=self.adapter_fn,
            payload=sfn.TaskInput.from_object(
                {
                    "mode": "complete",
                    "operation": sfn.JsonPath.object_at("$.run.step.pending.op"),
                    "result": sfn.JsonPath.object_at("$.run.step.pending.result"),
                    "proposal_id": sfn.JsonPath.string_at("$.run.proposal.proposal_id"),
                }
            ),
            payload_response_only=True,
            result_path="$.adapter",
        )  # writes the store only, idempotently, so the default service retries are safe
        complete.add_catch(self.error, errors=["States.ALL"], result_path="$.error")
        completed = self._task(
            f"Completed{n}",
            Phase.COMPLETED,
            catch=True,
            result=sfn.JsonPath.object_at("$.adapter.result"),
        )
        resend_wait = sfn.Wait(
            self.scope,
            self._id(f"ResendWait{n}"),
            state_name=f"ResendWait{n}",
            time=sfn.WaitTime.seconds_path("$.run.step.pending.wait_s"),
        )
        finality_wait = sfn.Wait(
            self.scope,
            self._id(f"FinalityWait{n}"),
            state_name=f"FinalityWait{n}",
            time=sfn.WaitTime.seconds_path("$.run.step.wait_s"),
        )
        check = self._task(f"FinalityCheck{n}", Phase.FINALITY_CHECK, catch=True)

        build.next(route)
        route.when(sfn.Condition.is_not_null("$.run.step.pending"), dispatch)
        route.when(sfn.Condition.string_equals("$.run.step.stage", "finality"), finality_wait)
        route.otherwise(after)
        dispatch.when(
            sfn.Condition.string_equals("$.run.step.pending.action", "complete"), complete
        )
        dispatch.when(
            sfn.Condition.string_equals("$.run.step.pending.action", "resend"), resend_wait
        )
        dispatch.otherwise(adapter)
        resend_wait.next(adapter)
        adapter.next(result)
        result.next(route)
        crashed.next(route)
        complete.next(completed)
        completed.next(route)
        finality_wait.next(check)
        check.next(route)
        return build


def grant_start(workflow: KitWorkflow, grantee: iam.IGrantable) -> None:
    """For the agent plane's propose tools: start this kit's executions and nothing else."""
    workflow.state_machine.grant_start_execution(grantee)
