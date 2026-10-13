"""awx-deploy: the operator deploy runner (docs/deployment.md has the runbook).

One operator CLI that loads a deployed kit's state. A deployed kit runs no setup code, so every
piece of state a kit needs reaches it through here, with operator AWS credentials:

    awx-deploy [--profile P] [--region R] tenants put <file> [--replace] [--var K=V ...]
    awx-deploy ... run <kit> <command> <args.json> [--var K=V ...]
    awx-deploy ... plan <deploy-file> [--var K=V ...] [--vars vars.json]
    awx-deploy ... apply <deploy-file> [--var K=V ...] [--vars vars.json] [--from N]
    awx-deploy ... trigger <kit> <action> <intent.json> --tenant <tenant_id>

`--region` defaults to the Region recorded in infra/cdk-outputs.json, else
awx_common.regions.DEFAULT_REGION.

- `tenants put` writes kit-bound tenants rows. Every row passes `tenant_from_row`; a
  connected account may appear on one row only, in any kit (an `account#<acct>` marker item is
  written in the same transaction, and existing rows are scanned too); rows are written
  conditionally, and changing a row needs `--replace` and quiet state machines in its kit(s).
- `run` runs one kit operator command (the kit's OPERATOR_COMMANDS) through
  `run_operator_command`. `store_for(t)` resolves `t` with
  `resolve_in_kit(DynamoDbTenantResolver, t, kit)` first; `read` is a platform-scope GET
  through awx_spine's client, keyed from the deployed Airwallex secret. A command whose args
  carry `replace: true` is refused while any of the kit's executions is RUNNING.
- `plan` prints a per-kit deploy file's steps and writes nothing; `apply` runs them in order
  and stops at the first refusal. Unresolved `${VAR}`s and `<placeholder>`s are refused.
- `trigger` proposes a schedule-driven action (origin SCHEDULE) through the deployed engine
  and starts its execution. The deployment has no EventBridge schedules, so no money moves
  unattended in the shared sandbox; production would use EventBridge Scheduler.

Nothing here moves money: sandbox deposits stay manual steps, run under the shared sandbox
lock.
"""

from __future__ import annotations

import argparse
import importlib
import json
import re
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from awx_common import transport
from awx_common.approvals import Origin
from awx_common.errors import TenantScopeViolation
from awx_common.kits import KIT_DIRS
from awx_common.operator_commands import OperatorCommandError, OperatorContext, run_operator_command
from awx_common.ops import ReadRequest
from awx_common.regions import DEFAULT_REGION
from awx_common.store import KitStore
from awx_common.tenancy import (
    ROLE_CUSTOMER,
    ROLE_OPERATOR,
    TenantContext,
    resolve_in_kit,
    tenant_from_row,
)

from . import engine
from .kit_store import DynamoDbKitStore
from .repos import DynamoDbTenantResolver

SPINE_STACK = "AwxSpine"
# The file `make deploy` writes (cdk deploy --outputs-file, run from infra/).
OUTPUTS_FILE = Path(__file__).resolve().parents[4] / "infra" / "cdk-outputs.json"


def resolve_region(region: str | None, outputs_file: Path | None = None) -> str:
    """--region, else the Region recorded in the outputs file (AwxAgentStack.AwxRegion), else
    awx_common.regions.DEFAULT_REGION."""
    if region:
        return region
    outputs_file = outputs_file or OUTPUTS_FILE
    if outputs_file.is_file():
        try:
            recorded = json.loads(outputs_file.read_text())["AwxAgentStack"]["AwxRegion"]
        except (ValueError, KeyError, TypeError) as exc:
            raise DeployError(
                f"{outputs_file}: no AwxAgentStack.AwxRegion ({exc!r}); pass --region"
            ) from None
        return str(recorded)
    return DEFAULT_REGION


MACHINE_PREFIX = "awx-"  # infra.constructs.kit_workflow.MACHINE_PREFIX (awx-<kit>-<action>)
ACCOUNT_MARKER = "account#"  # tenants-table item holding one account's owner (never a tenant)
# Schedule-driven actions (origin SCHEDULE keeps the policy tier; the agent floor does not
# apply). Only these can be triggered: anything else is the agent's or a person's to propose.
SCHEDULED_ACTIONS: Mapping[str, frozenset[str]] = {
    "k6": frozenset({"run_payroll"}),
    "k7": frozenset({"collect_weekly_repayment", "reconcile_portfolio"}),
    "k8": frozenset({"settle_seller_cycle", "reconcile_seller_money"}),
}
TRIGGERED_BY = "awx-deploy:schedule"
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_PLACEHOLDER = re.compile(r"<[^<>]+>")


class DeployError(Exception):
    """A refusal: the runner wrote nothing for the step that raised it."""


# --- deployed resources -----------------------------------------------------------------------


@dataclass
class Deployment:
    """Where a deployment lives, and the clients to reach it. Every name comes from the stacks
    (`discover`) or from explicit flags; nothing is guessed."""

    tenants_table: str
    ledger_table: str
    secret_arn: str | None
    ddb: Any
    sfn: Any
    port: Any = None  # an AirwallexPort for platform reads; built from the secret if None
    secrets: Any = None  # the secretsmanager client of the same --profile/--region session
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    def reader(self) -> Any:
        if self.port is None:
            if not self.secret_arn:
                raise DeployError("no Airwallex secret: reads (and commands that check ids) refuse")
            from awx_spine.client import AirwallexClient

            from .credentials import SecretsManagerCredentials

            self.port = AirwallexClient(
                SecretsManagerCredentials(self.secret_arn, client=self.secrets)
            )
        return self.port

    def resolver(self) -> DynamoDbTenantResolver:
        return DynamoDbTenantResolver(self.tenants_table, client=self.ddb)

    def store(self, kit: str, tenant_id: str) -> KitStore:
        return DynamoDbKitStore(self.ledger_table, kit=kit, tenant_id=tenant_id, client=self.ddb)

    def machines(self, kit: str) -> list[str]:
        """Every state machine of the kit (root and child): awx-<kit>-*."""
        prefix = f"{MACHINE_PREFIX}{kit}-"
        out: list[str] = []
        for page in self.sfn.get_paginator("list_state_machines").paginate():
            out += [
                m["stateMachineArn"] for m in page["stateMachines"] if m["name"].startswith(prefix)
            ]
        return out

    def root_machine(self, kit: str, action: str) -> str:
        name = f"{MACHINE_PREFIX}{kit}-{action}"
        for page in self.sfn.get_paginator("list_state_machines").paginate():
            for m in page["stateMachines"]:
                if m["name"] == name:
                    return str(m["stateMachineArn"])
        raise DeployError(f"no deployed state machine {name}")

    def running(self, kit: str) -> list[str]:
        """A RUNNING execution of the kit (any machine of the kit), or []. Follows nextToken to
        the end of every listing: Step Functions may return an empty page with a token."""
        for arn in self.machines(kit):
            hit = self._first(arn, "RUNNING")
            if hit:
                return [hit]
        return []

    def _first(self, machine_arn: str, status: str) -> str | None:
        token: str | None = None
        while True:
            kw: dict[str, Any] = {"stateMachineArn": machine_arn, "statusFilter": status}
            if token:
                kw["nextToken"] = token
            resp = self.sfn.list_executions(**kw)
            for e in resp.get("executions", []):
                return str(e["executionArn"])
            token = resp.get("nextToken")
            if not token:
                return None

    def executions(self, machine_arn: str, status: str) -> Iterator[str]:
        token: str | None = None
        while True:
            kw: dict[str, Any] = {"stateMachineArn": machine_arn, "statusFilter": status}
            if token:
                kw["nextToken"] = token
            resp = self.sfn.list_executions(**kw)
            for e in resp.get("executions", []):
                yield str(e["executionArn"])
            token = resp.get("nextToken")
            if not token:
                return


def discover(
    cfn: Any,
    *,
    ddb: Any,
    sfn: Any,
    secrets: Any = None,
    spine_stack: str = SPINE_STACK,
    **overrides: Any,
) -> Deployment:
    """Table names and the secret ARN from the spine stack: its outputs (TenantsTableName,
    AirwallexSecretArn) and its resources (the Ledger table has no output, so it is found by
    its logical id). State machines are found by their fixed awx-<kit>-<action> names."""
    stack = cfn.describe_stacks(StackName=spine_stack)["Stacks"][0]
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
    ledger = overrides.get("ledger_table")
    if not ledger:
        found = [
            r["PhysicalResourceId"]
            for page in cfn.get_paginator("list_stack_resources").paginate(StackName=spine_stack)
            for r in page["StackResourceSummaries"]
            if r["ResourceType"] == "AWS::DynamoDB::Table"
            and r["LogicalResourceId"].startswith("LedgerTable")
        ]
        if len(found) != 1:
            raise DeployError(f"{spine_stack} has {len(found)} Ledger tables, expected 1")
        ledger = found[0]
    tenants = overrides.get("tenants_table") or outputs.get("TenantsTableName")
    if not tenants:
        raise DeployError(f"{spine_stack} has no TenantsTableName output")
    return Deployment(
        tenants_table=tenants,
        ledger_table=ledger,
        secret_arn=overrides.get("secret_arn") or outputs.get("AirwallexSecretArn"),
        ddb=ddb,
        sfn=sfn,
        secrets=secrets,
    )


# --- substitution and placeholders ------------------------------------------------------------


def substitute(value: Any, variables: Mapping[str, str]) -> Any:
    """Replace ${NAME} in every string; an unknown name is left in place (and then refused)."""
    if isinstance(value, str):
        return _VAR.sub(lambda m: variables.get(m.group(1), m.group(0)), value)
    if isinstance(value, Mapping):
        return {k: substitute(v, variables) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [substitute(v, variables) for v in value]
    return value


def placeholders(value: Any, where: str = "") -> list[str]:
    """Every unresolved ${VAR} or <placeholder> left in a value, with its path."""
    out: list[str] = []
    if isinstance(value, str):
        if _VAR.search(value) or _PLACEHOLDER.search(value):
            out.append(f"{where or '.'}: {value}")
    elif isinstance(value, Mapping):
        for k, v in value.items():
            out += placeholders(v, f"{where}.{k}")
    elif isinstance(value, list | tuple):
        for i, v in enumerate(value):
            out += placeholders(v, f"{where}[{i}]")
    return out


def _refuse_placeholders(value: Any, what: str) -> None:
    left = placeholders(value)
    if left:
        raise DeployError(f"{what} still holds placeholders: {'; '.join(left)}")


# --- the runner -------------------------------------------------------------------------------


def _kit_package(kit: str) -> str:
    if kit not in KIT_DIRS:
        raise DeployError(f"unknown kit {kit!r}")
    return "awx_" + KIT_DIRS[kit].rsplit("/", 1)[1]


def _commands(kit: str) -> tuple[Any, ...]:
    return tuple(getattr(importlib.import_module(_kit_package(kit)), "OPERATOR_COMMANDS", ()))


@dataclass(frozen=True)
class TenantRowIn:
    tenant_id: str
    kit: str
    role: str | None
    connected_account_id: str | None

    def item(self) -> dict[str, Any]:
        item: dict[str, Any] = {"tenant_id": {"S": self.tenant_id}, "kit": {"S": self.kit}}
        item["role"] = {
            "S": self.role
            or (ROLE_OPERATOR if self.connected_account_id is None else ROLE_CUSTOMER)
        }
        if self.connected_account_id is not None:
            item["connected_account_id"] = {"S": self.connected_account_id}
        return item


def parse_rows(raw: Any) -> list[TenantRowIn]:
    rows = raw.get("tenants", []) if isinstance(raw, Mapping) else raw
    if not isinstance(rows, list):
        raise DeployError("tenants must be a list of rows")
    out: list[TenantRowIn] = []
    for i, r in enumerate(rows):
        if not isinstance(r, Mapping) or set(r) - {
            "tenant_id",
            "kit",
            "role",
            "connected_account_id",
        }:
            raise DeployError(
                f"tenants[{i}]: a row is {{tenant_id, kit, role, connected_account_id}}"
            )
        tid, kit = r.get("tenant_id"), r.get("kit")
        if not isinstance(tid, str) or not tid or "#" in tid:
            raise DeployError(f"tenants[{i}]: tenant_id must be a string without '#'")
        if kit not in KIT_DIRS:
            raise DeployError(f"tenants[{i}]: kit must be one of {sorted(KIT_DIRS)}")
        out.append(TenantRowIn(tid, kit, r.get("role"), r.get("connected_account_id")))
    return out


class Runner:
    def __init__(self, deployment: Deployment, *, out: Callable[[str], None] = print) -> None:
        self.d = deployment
        self.out = out

    # --- tenants ------------------------------------------------------------------------------

    def _existing_rows(self) -> dict[str, Mapping[str, Any]]:
        rows: dict[str, Mapping[str, Any]] = {}
        kwargs: dict[str, Any] = {"TableName": self.d.tenants_table, "ConsistentRead": True}
        while True:
            resp = self.d.ddb.scan(**kwargs)
            for item in resp.get("Items", []):
                rows[item["tenant_id"]["S"]] = item
            if "LastEvaluatedKey" not in resp:
                return rows
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    def tenants_put(
        self, rows: Sequence[TenantRowIn], *, replace_rows: bool = False
    ) -> dict[str, list[str]]:
        """Validate every row and every account first, then write row by row (each with its
        account marker, in one transaction). Returns {written, unchanged}."""
        _refuse_placeholders([r.__dict__ for r in rows], "the tenants rows")
        seen_ids: set[str] = set()
        seen_acct: dict[str, str] = {}
        for r in rows:
            try:  # the kit-bound row rule, in the row's own kit: kit, role, the acct_ match
                tenant_from_row(
                    r.tenant_id,
                    kit=r.kit,
                    row_kit=r.kit,
                    account=r.connected_account_id,
                    role=r.role,
                )
            except TenantScopeViolation as exc:
                raise DeployError(f"tenants row {r.tenant_id}: {exc.args[0]}") from None
            if r.tenant_id in seen_ids:
                raise DeployError(f"tenants row {r.tenant_id} appears twice")
            seen_ids.add(r.tenant_id)
            if r.connected_account_id is not None:
                if r.connected_account_id in seen_acct:
                    raise DeployError(
                        f"rows {seen_acct[r.connected_account_id]} and {r.tenant_id} share one "
                        "connected account"
                    )
                seen_acct[r.connected_account_id] = r.tenant_id
        existing = self._existing_rows()
        owners = {
            item["connected_account_id"].get("S"): tid
            for tid, item in existing.items()
            if not tid.startswith(ACCOUNT_MARKER) and "connected_account_id" in item
        }
        owners.update(
            {
                tid[len(ACCOUNT_MARKER) :]: item.get("owner", {}).get("S", "?")
                for tid, item in existing.items()
                if tid.startswith(ACCOUNT_MARKER)
            }
        )
        plan: list[tuple[TenantRowIn, Mapping[str, Any] | None]] = []
        for r in rows:
            acct = r.connected_account_id
            if acct is not None and owners.get(acct) not in (None, r.tenant_id):
                raise DeployError(
                    f"tenants row {r.tenant_id}: its connected account is already on row "
                    f"{owners[acct]} (one account, one row, in every kit)"
                )
            old = existing.get(r.tenant_id)
            if old is not None and _same_row(old, r):
                plan.append((r, old))
                continue
            if old is not None:
                if not replace_rows:
                    raise DeployError(
                        f"tenants row {r.tenant_id} exists and differs; pass --replace"
                    )
                for kit in sorted({r.kit, old.get("kit", {}).get("S", r.kit)}):
                    self.require_quiet(kit, f"replacing tenants row {r.tenant_id}")
            plan.append((r, old))
        written, unchanged = [], []
        for r, old in plan:
            if old is not None and _same_row(old, r):
                unchanged.append(r.tenant_id)
                continue
            self._write_row(r, old)
            written.append(r.tenant_id)
        return {"written": written, "unchanged": unchanged}

    def _write_row(self, r: TenantRowIn, old: Mapping[str, Any] | None) -> None:
        table = self.d.tenants_table
        items: list[dict[str, Any]] = []
        if old is None:
            items.append(
                {
                    "Put": {
                        "TableName": table,
                        "Item": r.item(),
                        "ConditionExpression": "attribute_not_exists(tenant_id)",
                    }
                }
            )
        else:  # replace exactly the row we read; a concurrent change fails the transaction
            names, values, conds = {}, {}, []
            for i, (k, v) in enumerate(sorted(old.items())):
                names[f"#a{i}"] = k
                values[f":v{i}"] = v
                conds.append(f"#a{i} = :v{i}")
            if "connected_account_id" not in old:
                names["#acct"] = "connected_account_id"
                conds.append("attribute_not_exists(#acct)")
            items.append(
                {
                    "Put": {
                        "TableName": table,
                        "Item": r.item(),
                        "ConditionExpression": " AND ".join(conds),
                        "ExpressionAttributeNames": names,
                        "ExpressionAttributeValues": values,
                    }
                }
            )
            old_acct = old.get("connected_account_id", {}).get("S")
            if old_acct and old_acct != r.connected_account_id:
                items.append(
                    {
                        "Delete": {
                            "TableName": table,
                            "Key": {"tenant_id": {"S": ACCOUNT_MARKER + old_acct}},
                            "ConditionExpression": "attribute_not_exists(tenant_id) OR #o = :o",
                            "ExpressionAttributeNames": {"#o": "owner"},
                            "ExpressionAttributeValues": {":o": {"S": r.tenant_id}},
                        }
                    }
                )
        acct = r.connected_account_id
        old_acct = (old or {}).get("connected_account_id", {}).get("S")
        if acct is not None and acct != old_acct:
            items.append(
                {
                    "Put": {
                        "TableName": table,
                        "Item": {
                            "tenant_id": {"S": ACCOUNT_MARKER + acct},
                            "owner": {"S": r.tenant_id},
                        },
                        "ConditionExpression": "attribute_not_exists(tenant_id)",
                    }
                }
            )
        try:
            self.d.ddb.transact_write_items(TransactItems=items)
        except Exception as exc:
            if "TransactionCanceled" in type(exc).__name__ or "ConditionalCheckFailed" in str(exc):
                raise DeployError(
                    f"tenants row {r.tenant_id}: another write landed first, or its account was "
                    "taken meanwhile; nothing was written"
                ) from None
            raise

    # --- quiet check --------------------------------------------------------------------------

    def require_quiet(self, kit: str, what: str) -> None:
        running = self.d.running(kit)
        if running:
            raise DeployError(
                f"{what} refused: {len(running)} {kit} execution(s) RUNNING "
                f"(a replace runs only while the kit's state machines are quiet)"
            )

    # --- operator commands --------------------------------------------------------------------

    def context(self, kit: str, *, online: bool = True) -> OperatorContext:
        resolver = self.d.resolver()

        def store_for(tenant_id: str) -> KitStore:
            tenant = resolve_in_kit(resolver, tenant_id, kit)  # this kit's row only
            return self.d.store(kit, tenant.tenant_id)

        read = None
        if online:
            port = self.d.reader()

            def read(req: ReadRequest) -> Mapping[str, Any]:
                if req.on_behalf_of is not None:
                    raise OperatorCommandError("an operator command reads at platform scope only")
                return port.read(req)

        return OperatorContext(
            kit=kit,
            platform_store=self.d.store(kit, engine.PLATFORM_TENANT_ID),
            store_for=store_for,
            now=self.d.clock(),
            read=read,
        )

    def run_command(self, kit: str, name: str, args: Mapping[str, Any]) -> Mapping[str, Any]:
        if not isinstance(args, Mapping):
            raise DeployError("command arguments must be a JSON object")
        _refuse_placeholders(args, f"{kit} {name} args")
        commands = _commands(kit)
        if not commands:
            raise DeployError(f"kit {kit} has no operator commands")
        if args.get("replace") is True:
            self.require_quiet(kit, f"{kit} {name} with replace")
        try:
            return run_operator_command(commands, name, self.context(kit), args)
        except (OperatorCommandError, TenantScopeViolation) as exc:
            raise DeployError(f"{kit} {name} refused: {exc.args[0] if exc.args else exc}") from None

    # --- deploy files -------------------------------------------------------------------------

    def steps(self, doc: Mapping[str, Any]) -> list[tuple[str, Any]]:
        kit = doc.get("kit")
        if kit not in KIT_DIRS:
            raise DeployError("a deploy file names its kit (k1 to k8)")
        out: list[tuple[str, Any]] = []
        rows = doc.get("tenants") or []
        if rows:
            parsed = parse_rows(rows)
            other = sorted({r.kit for r in parsed} - {kit})
            if other:
                raise DeployError(
                    f"a {kit} deploy file may hold only {kit} tenants rows, not {other}"
                )
            out.append(("tenants", parsed))
        for i, step in enumerate(doc.get("steps") or []):
            if (
                not isinstance(step, Mapping)
                or set(step) - {"command", "args", "note"}
                or "command" not in step
            ):
                raise DeployError(f"steps[{i}] is {{command, args, note?}}")
            out.append(("run", (step["command"], dict(step.get("args") or {}))))
        return out

    def plan(self, doc: Mapping[str, Any]) -> int:
        """Print every step; write nothing. Returns the number of unresolved placeholders."""
        kit = doc.get("kit")
        missing = 0
        for n, (kind, body) in enumerate(self.steps(doc), 1):
            if kind == "tenants":
                self.out(f"{n}. tenants put ({len(body)} rows)")
                for r in body:
                    left = placeholders(r.__dict__)
                    missing += len(left)
                    acct = r.connected_account_id or "(no account: operator)"
                    flag = "  UNRESOLVED" if left else ""
                    self.out(f"     {r.tenant_id}  kit={r.kit}  role={r.role or '-'}  {acct}{flag}")
            else:
                name, args = body
                left = placeholders(args)
                missing += len(left)
                rep = (
                    "  (replace: needs quiet state machines)" if args.get("replace") is True else ""
                )
                self.out(f"{n}. run {kit} {name}{rep}")
                self.out("     " + json.dumps(args, sort_keys=True))
                for p in left:
                    self.out(f"     UNRESOLVED {p}")
        return missing

    def apply(self, doc: Mapping[str, Any], *, start: int = 1) -> list[str]:
        """Run the steps in order from `start` (1-based); stop at the first refusal."""
        kit = str(doc.get("kit"))
        steps = self.steps(doc)
        for kind, body in steps:  # refuse a file with any placeholder before writing anything
            _refuse_placeholders(
                body[1] if kind == "run" else [r.__dict__ for r in body], f"step {kind}"
            )
        done: list[str] = []
        for n, (kind, body) in enumerate(steps, 1):
            if n < start:
                continue
            try:
                if kind == "tenants":
                    rep = self.tenants_put(body)
                    self.out(
                        f"{n}. tenants: written {rep['written']}, unchanged {rep['unchanged']}"
                    )
                else:
                    name, args = body
                    report = self.run_command(kit, name, args)
                    self.out(f"{n}. {name}: {json.dumps(report, sort_keys=True, default=str)}")
            except DeployError as exc:
                raise DeployError(
                    f"step {n} refused, stopping (resume with --from {n}): {exc}"
                ) from None
            done.append(f"{n}")
        return done

    # --- triggers -----------------------------------------------------------------------------

    def trigger(
        self, kit: str, action: str, intent: Mapping[str, Any], *, tenant_id: str
    ) -> Mapping[str, Any]:
        if action not in SCHEDULED_ACTIONS.get(kit, frozenset()):
            raise DeployError(
                f"{kit}.{action} is not schedule-driven; scheduled actions: "
                f"{ {k: sorted(v) for k, v in SCHEDULED_ACTIONS.items()} }"
            )
        _refuse_placeholders(intent, "the intent")
        spec = importlib.import_module(_kit_package(kit)).SPECS[action]
        arn = self.d.root_machine(kit, action)
        env = DeployEnv(self.d, kit)
        try:
            tenant = env.resolve_tenant(tenant_id)
            proposal, decision = engine.propose(
                spec,
                (),
                intent,
                proposed_by=TRIGGERED_BY,
                tenant=tenant,
                env=env,
                origin=Origin.SCHEDULE,
            )
        except TenantScopeViolation as exc:
            raise DeployError(f"trigger refused: {exc.args[0]}") from None
        execution = engine.execution_input(proposal, decision, intent, tenant.tenant_id)
        prior = self._same_intent(arn, proposal.intent_hash)
        if prior is not None:
            return {
                "proposal_id": proposal.proposal_id,
                "risk_tier": proposal.risk_tier.value,
                "reasons": list(proposal.reasons),
                "started": False,
                "existing_execution": prior[0],
                "existing_status": prior[1],
            }
        try:
            self.d.sfn.start_execution(
                stateMachineArn=arn, name=proposal.proposal_id, input=transport.dumps(execution)
            )
            started = True
        except Exception as exc:
            if "ExecutionAlreadyExists" not in (type(exc).__name__ + str(exc)):
                raise
            started = False  # the same proposal is already running or done: nothing new
        return {
            "proposal_id": proposal.proposal_id,
            "risk_tier": proposal.risk_tier.value,
            "reasons": list(proposal.reasons),
            "started": started,
        }

    def _same_intent(self, machine_arn: str, want: str) -> tuple[str, str] | None:
        """A RUNNING root execution whose proposal carries this intent hash, from any approval
        window. Only RUNNING counts: a FAILED, BLOCKED, REJECTED or EXPIRED run also ends as a
        SUCCEEDED execution, so a finished one never blocks a re-trigger (within one window the
        engine's proposal id already makes the start a no-op). A RUNNING execution whose input
        cannot be read might be the same intent, so the trigger refuses, naming it."""
        for arn in self.d.executions(machine_arn, "RUNNING"):
            try:
                doc = transport.loads(self.d.sfn.describe_execution(executionArn=arn)["input"])
                p = doc.get("proposal") if isinstance(doc, Mapping) else None
                if isinstance(p, Mapping):
                    got = p.get("intent_hash")
                else:
                    got = getattr(p, "intent_hash", None)
                if not isinstance(got, str):
                    raise ValueError("no proposal intent hash")
            except Exception as exc:
                raise DeployError(
                    f"trigger refused: RUNNING execution {arn} has unreadable input "
                    f"({type(exc).__name__}); it might be the same intent. Check it, then retry"
                ) from None
            if got == want:
                return arn, "RUNNING"
        return None


def _same_row(item: Mapping[str, Any], r: TenantRowIn) -> bool:
    want = r.item()
    return {k: v for k, v in item.items()} == want


@dataclass
class DeployEnv:
    """engine.Env for `trigger`: the deployed stores, the deployed tenants table (in this kit
    only) and platform-scope or tenant reads through awx_spine's client (one retry layer: the
    engine's). No run_status: the run decides at recheck."""

    d: Deployment
    kit: str
    sandbox: bool = True

    def now(self) -> datetime:
        return self.d.clock()

    def sleep(self, seconds: float) -> None:
        import time

        time.sleep(seconds)

    def read_budget_s(self) -> float | None:
        return 60.0

    def read(self, kit: str, req: ReadRequest, tenant: TenantContext) -> Mapping[str, Any]:
        return engine.read_once(self.d.reader(), req)

    def read_platform(self, kit: str, req: ReadRequest, tenant: TenantContext) -> Mapping[str, Any]:
        return engine.read_once(self.d.reader(), req)

    def store(self, kit: str, tenant: TenantContext) -> KitStore:
        return self.d.store(kit, tenant.tenant_id)

    def resolve_tenant(self, tenant_id: str) -> TenantContext:
        return resolve_in_kit(self.d.resolver(), tenant_id, self.kit)


# --- CLI --------------------------------------------------------------------------------------


def load_file(path: str) -> Any:
    text = Path(path).read_text()
    if path.endswith((".yaml", ".yml")):
        import yaml

        return yaml.safe_load(text)
    return json.loads(text)


def _variables(args: argparse.Namespace) -> dict[str, str]:
    out: dict[str, str] = {}
    for f in args.vars or []:
        loaded = load_file(f)
        if not isinstance(loaded, Mapping):
            raise DeployError(f"{f}: a vars file is a mapping of NAME: value")
        out.update({str(k): str(v) for k, v in loaded.items()})
    for kv in args.var or []:
        name, sep, value = kv.partition("=")
        if not sep or not name:
            raise DeployError(f"--var {kv!r}: use NAME=value")
        out[name] = value
    return out


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="awx-deploy", description=__doc__.split("\n\n")[0])
    p.add_argument("--profile")
    p.add_argument(
        "--region",
        help="the deployment's Region (default: the Region in infra/cdk-outputs.json, "
        f"else {DEFAULT_REGION})",
    )
    p.add_argument("--spine-stack", default=SPINE_STACK)
    p.add_argument("--tenants-table")
    p.add_argument("--ledger-table")
    p.add_argument("--secret-arn")
    sub = p.add_subparsers(dest="cmd", required=True)

    def with_vars(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--var", action="append", metavar="NAME=VALUE")
        sp.add_argument("--vars", action="append", metavar="FILE")

    t = sub.add_parser("tenants", help="tenants-table rows (each bound to one kit)")
    tsub = t.add_subparsers(dest="tcmd", required=True)
    tp = tsub.add_parser("put", help="write rows from a file ({tenants: [...]} or a list)")
    tp.add_argument("file")
    tp.add_argument("--replace", action="store_true")
    with_vars(tp)
    r = sub.add_parser("run", help="run one kit operator command")
    r.add_argument("kit")
    r.add_argument("command")
    r.add_argument("args_file", metavar="args.json")
    with_vars(r)
    pl = sub.add_parser("plan", help="print a deploy file's steps; write nothing")
    pl.add_argument("file")
    with_vars(pl)
    ap = sub.add_parser("apply", help="run a deploy file's steps in order")
    ap.add_argument("file")
    ap.add_argument("--from", dest="start", type=int, default=1)
    with_vars(ap)
    tr = sub.add_parser("trigger", help="propose a schedule-driven action (origin SCHEDULE)")
    tr.add_argument("kit")
    tr.add_argument("action")
    tr.add_argument("intent_file", metavar="intent.json")
    tr.add_argument("--tenant", required=True)
    return p


def deployment_from_args(args: argparse.Namespace) -> Deployment:
    import boto3

    session = boto3.Session(profile_name=args.profile, region_name=resolve_region(args.region))
    return discover(
        session.client("cloudformation"),
        ddb=session.client("dynamodb"),
        sfn=session.client("stepfunctions"),
        secrets=session.client("secretsmanager"),
        spine_stack=args.spine_stack,
        tenants_table=args.tenants_table,
        ledger_table=args.ledger_table,
        secret_arn=args.secret_arn,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    deployment: Deployment | None = None,
    out: Callable[[str], None] = print,
) -> int:
    args = parser().parse_args(argv)
    try:
        variables = _variables(args) if hasattr(args, "var") else {}
        if args.cmd == "plan":  # writes nothing, needs no AWS
            doc = substitute(load_file(args.file), variables)
            missing = Runner(deployment or _NoDeployment(), out=out).plan(doc)
            if missing:
                out(f"{missing} unresolved placeholder(s): apply would refuse")
                return 2
            return 0
        d = deployment or deployment_from_args(args)
        runner = Runner(d, out=out)
        if args.cmd == "tenants":
            rows = parse_rows(substitute(load_file(args.file), variables))
            out(json.dumps(runner.tenants_put(rows, replace_rows=args.replace), sort_keys=True))
        elif args.cmd == "run":
            report = runner.run_command(
                args.kit, args.command, substitute(load_file(args.args_file), variables)
            )
            out(json.dumps(report, sort_keys=True, default=str))
        elif args.cmd == "apply":
            runner.apply(substitute(load_file(args.file), variables), start=args.start)
        elif args.cmd == "trigger":
            out(
                json.dumps(
                    runner.trigger(
                        args.kit, args.action, load_file(args.intent_file), tenant_id=args.tenant
                    ),
                    sort_keys=True,
                )
            )
        return 0
    except DeployError as exc:
        out(f"refused: {exc}")
        return 1


class _NoDeployment(Deployment):
    def __init__(self) -> None:  # plan never touches AWS
        super().__init__(tenants_table="", ledger_table="", secret_arn=None, ddb=None, sfn=None)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
