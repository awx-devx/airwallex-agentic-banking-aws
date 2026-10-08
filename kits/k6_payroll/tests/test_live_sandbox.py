"""Kit 6 live acceptance run on the shared sandbox, opt-in (marker `sandbox`).

Kit 6's required acceptance scenario with the amounts of test_acceptance_sandbox.py, through
AirwallexClient and awx_spine_aws.local_runner: fund a 20000 USD, b exactly 0.01 USD short of
its payroll, c 5000 USD (deposit simulation into each employer's USD Global Account, reused if
one exists); AUTO rate lock; the approved payroll; when a's first payout is created, 1000 USD
lands in c (the mid-run deposit). Expect a and c SUCCEEDED, b BLOCKED with nothing sent for b
and b's balance unchanged, and every transfer, conversion and charge this run sent terminal.

Runs live only with AWX_K6_LIVE=1, and then needs AWX_K6_ACCOUNTS: the path of the owner-only
connected-accounts file (K6_EMPLOYER_A_ACCT, _B_, _C_). It NEVER creates a connected account:
a missing or non-ACTIVE account stops the run. Every sandbox call runs under
flock /tmp/awx-sandbox.lock, held for the whole run. The client loads the key itself; nothing
here prints or logs it. AWX_K6_RECORD (optional) names a file for the run record (mode 600; it
holds account ids, so keep it out of the repo). Without AWX_K6_LIVE the same code runs on
PayrollFake.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from awx_common import (
    ApprovalDecision,
    Decision,
    Money,
    Operation,
    Origin,
    ReadRequest,
)
from awx_common.store import InMemoryKitStore
from awx_common.tenancy import StaticTenantResolver, TenantRow
from awx_k6_payroll import PAYROLL_SPEC, SPECS
from awx_k6_payroll.books import read_book
from awx_k6_payroll.workflow import LOCK_SPEC
from awx_spine.store import InMemoryOperationStore
from awx_spine_aws.local_runner import RunRegistry, run_kit_workflow
from k6_data import PERIOD, batch

pytestmark = pytest.mark.sandbox

LIVE = os.environ.get("AWX_K6_LIVE") == "1"
LOCK = "/tmp/awx-sandbox.lock"  # noqa: S108
IDS = os.environ.get("AWX_K6_ACCOUNTS")
RECORD = os.environ.get("AWX_K6_RECORD")
MONEY_OPS = {"fx.conversions.create", "transfers.create", "charges.create", "beneficiaries.create"}
IN_FLIGHT = {"SCHEDULED", "PROCESSING", "SENT", "PENDING"}
TARGET = {"a": Decimal("20000"), "c": Decimal("5000")}
MID_RUN = Decimal("1000.00")

log: list[dict[str, Any]] = []


def note(**kw: Any) -> None:
    kw["t"] = datetime.now(UTC).isoformat(timespec="seconds")
    log.append(kw)
    print(json.dumps(kw, default=str), flush=True)


class Port:
    """The client (or a PayrollFake) plus the test's mid-run deposit hook and a send log."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.sent: list[tuple[Operation, Any]] = []
        self.after_write: dict[str, Any] = {}

    def execute(self, op: Operation):
        res = self.inner.execute(op)
        self.sent.append((op, res))
        hook = self.after_write.get(op.name)
        if hook is not None:
            hook(op)
        return res

    def read(self, req: ReadRequest):
        return self.inner.read(req)


def load_ids() -> dict[str, str]:
    if not IDS:
        raise SystemExit("STOP: AWX_K6_ACCOUNTS is not set; never create a connected account")
    keys = {"a": "K6_EMPLOYER_A_ACCT", "b": "K6_EMPLOYER_B_ACCT", "c": "K6_EMPLOYER_C_ACCT"}
    found: dict[str, str] = {}
    for line in Path(IDS).read_text().splitlines():
        k, _, rest = line.partition(":")
        for code, key in keys.items():
            if k.strip() == key:
                found[code] = rest.split("#")[0].strip()
    missing = sorted(set(keys) - set(found))
    if missing:
        raise SystemExit(f"STOP: no account id for employer(s) {missing}; never create one")
    return found


def usd_available(port: Port, acct: str) -> Decimal:
    body = port.read(ReadRequest("/api/v1/balances/current", {}, on_behalf_of=acct))
    rows = body if isinstance(body, list) else body.get("items", body.get("data", []))
    for r in rows or []:
        if r.get("currency") == "USD":
            return Decimal(str(r.get("available_amount", "0")))
    return Decimal("0")


def check_active(port: Port, code: str, acct: str) -> None:
    if not LIVE:
        return
    body = port.read(ReadRequest(f"/api/v1/accounts/{acct}", {}))
    status = body.get("status")
    nick = body.get("nickname") or (body.get("account_details") or {}).get("nickname")
    note(step="account", employer=code, status=status, nickname=nick)
    if status != "ACTIVE":
        raise SystemExit(f"STOP: poc-i6-{code} reads {status}, not ACTIVE")


def usd_global_account(port: Port, code: str, acct: str, run_id: str) -> str:
    def find() -> str | None:
        body = port.read(ReadRequest("/api/v1/global_accounts", {}, on_behalf_of=acct))
        for ga in body.get("items") or []:
            feats = (ga.get("required_features") or []) + (ga.get("supported_features") or [])
            ccys = {f.get("currency") for f in feats} | {ga.get("currency")}
            if "USD" in ccys and ga.get("status", "ACTIVE") in ("ACTIVE", "OPEN"):
                return str(ga["id"])
        return None

    got = find()
    if got is not None:
        note(step="global_account", employer=code, reused=True)
        return got
    rid = f"poc-i6-ga-{code}-{run_id}"[:50]
    op = Operation(
        kit="k6",
        name="global_accounts.create",
        method="POST",
        path="/api/v1/global_accounts/create",
        body={
            "request_id": rid,
            "country_code": "US",
            "required_features": [{"currency": "USD", "transfer_method": "LOCAL"}],
        },
        request_id=rid,
        business_key=f"poc-i6-ga:{code}",
        intent_hash="0" * 64,
        on_behalf_of=acct,
    )
    res = port.execute(op)
    note(
        step="global_account",
        employer=code,
        created=True,
        status=str(res.status),
        error=res.error_code,
    )
    for _ in range(15):
        got = find()
        if got is not None:
            return got
        time.sleep(2)
    raise SystemExit(
        f"STOP: poc-i6-{code} Global Account not listed after create ({res.error_code})"
    )


def deposit(port: Port, code: str, acct: str, ga: str, amount: Decimal) -> None:
    rid = f"poc-i6-dep-{uuid.uuid4().hex[:12]}"
    op = Operation(
        kit="k6",
        name="deposits.simulate",
        method="POST",
        path="/api/v1/simulation/deposit/create",
        body={"global_account_id": ga, "amount": amount, "payer_name": "poc-i6-payer"},
        request_id=rid,
        business_key=f"poc-i6-dep:{code}:{rid}",
        intent_hash="0" * 64,
        on_behalf_of=acct,
    )
    res = port.execute(op)
    note(
        step="deposit",
        employer=code,
        amount=str(amount),
        status=str(res.status),
        error=res.error_code,
    )
    if res.error_code:
        raise SystemExit(f"STOP: deposit into poc-i6-{code} refused ({res.error_code})")


class Run:
    def __init__(self, port: Port, accts: dict[str, str]) -> None:
        self.port, self.accts = port, accts
        self.ops = InMemoryOperationStore()
        self.kit_store, self.platform_store = InMemoryKitStore(), InMemoryKitStore()
        self.runs = RunRegistry()
        self.audit: list[Any] = []
        rows = {"k6:operator": TenantRow("k6", None, "operator")}
        rows |= {f"k6:employer:{c}": TenantRow("k6", a, "customer") for c, a in accts.items()}
        self.tenants = StaticTenantResolver(rows)
        self.now = (lambda: datetime.now(UTC)) if LIVE else port.inner.now
        self.sleep = time.sleep if LIVE else port.inner.advance

    def __call__(
        self, spec: Any, raw: Any, approvers: tuple[str, ...] = ("approver-1", "approver-2")
    ):
        def decide(p):
            return [
                ApprovalDecision(p.proposal_id, p.intent_hash, a, Decision.APPROVE, self.now())
                for a in approvers
            ]

        return run_kit_workflow(
            spec,
            raw,
            proposed_by="scheduler",
            tenant=self.tenants.resolve("k6:operator", kit="k6"),
            port=self.port,
            store=self.ops,
            decide=decide,
            now=self.now,
            sleep=self.sleep,
            audit=self.audit.append,
            kit_store=self.kit_store,
            platform_store=self.platform_store,
            tenants=self.tenants,
            origin=Origin.SCHEDULE,
            runs=self.runs,
            actions=tuple(SPECS),
        )


def acceptance() -> tuple[str, list[str]]:
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    t0 = time.time()
    outcome = "FAIL"
    problems: list[str] = []
    if LIVE:
        from awx_spine import AirwallexClient, EnvFileCredentials

        port = Port(AirwallexClient(EnvFileCredentials()))
        accts = load_ids()
    else:
        from k6_fake import PayrollFake

        fake = PayrollFake()
        port = Port(fake)
        accts = {c: fake.employer(c, "0") for c in "abc"}
    fh = open(LOCK, "a")
    fcntl.flock(fh, fcntl.LOCK_EX)
    note(step="lock", held=True, live=LIVE, run_id=run_id)
    try:
        for code, acct in accts.items():
            check_active(port, code, acct)
        gas = {c: usd_global_account(port, c, a, run_id) for c, a in accts.items()}
        before = {c: usd_available(port, a) for c, a in accts.items()}
        note(step="balances_before", **{c: str(v) for c, v in before.items()})

        r = Run(port, accts)
        lock = r(LOCK_SPEC, batch())
        note(step="lock_rates", state=lock.state, detail=lock.detail)
        if lock.state != "SUCCEEDED":
            raise SystemExit("STOP: rate lock did not succeed")
        probe = r(
            PAYROLL_SPEC,
            {"pay_period": PERIOD, "employers": [batch()["employers"][1]]},
            approvers=(),
        )
        ch = probe.proposal.computed["children"][0]
        sells = sum((v["sell"] for v in ch["fx_lock"].values()), Money.zero("USD"))
        need_b = sells + ch["pricing"]["by_currency"]["USD"]["need"] + ch["pricing"]["platform_fee"]
        short = need_b.amount - Decimal("0.01")
        note(step="need_b", state=probe.state, need=str(need_b.amount), short=str(short))
        if probe.state != "EXPIRED" or any(
            o.on_behalf_of == accts["b"] and o.name in MONEY_OPS for o, _ in port.sent
        ):
            raise SystemExit("STOP: the pricing probe did more than price")

        targets = {**TARGET, "b": short}
        for code in "abc":
            gap = targets[code] - before[code]
            if code == "b" and gap < 0:
                raise SystemExit(f"STOP: poc-i6-b already holds {before['b']} USD, above {short}")
            if gap > 0:
                deposit(port, code, accts[code], gas[code], gap)
        funded = {c: usd_available(port, a) for c, a in accts.items()}
        note(step="balances_funded", **{c: str(v) for c, v in funded.items()})
        if funded["b"] != short:
            raise SystemExit(f"STOP: poc-i6-b reads {funded['b']}, not {short}")

        fired: list[bool] = []

        def mid_run(op: Operation) -> None:
            if op.on_behalf_of == accts["a"] and not fired:
                fired.append(True)
                deposit(port, "c", accts["c"], gas["c"], MID_RUN)

        port.after_write["transfers.create"] = mid_run
        sent_before = len(port.sent)
        out = r(PAYROLL_SPEC, batch())
        kids = {c.proposal.intent["employer"]: c for c in out.child_outcomes}
        states = {k: kids[k].state for k in "abc" if k in kids}
        note(
            step="payroll",
            state=out.state,
            children=states,
            detail=out.detail,
            child_detail={k: kids[k].detail for k in kids},
        )
        ops = [(o, res) for o, res in port.sent[sent_before:]]

        if out.state != "SUCCEEDED":
            problems.append(f"root {out.state}: {out.detail}")
        if states != {"a": "SUCCEEDED", "b": "BLOCKED", "c": "SUCCEEDED"}:
            problems.append(f"children {states}")
        b_money = [
            o.name
            for o, _ in ops
            if o.name in MONEY_OPS
            and (o.on_behalf_of == accts["b"] or accts["b"] in json.dumps(o.body, default=str))
        ]
        if b_money:
            problems.append(f"money ops for b: {b_money}")
        after = {c: usd_available(port, a) for c, a in accts.items()}
        note(step="balances_after", **{c: str(v) for c, v in after.items()})
        if after["b"] != short:
            problems.append(f"b balance moved: {after['b']} != {short}")
        if not fired:
            problems.append("mid-run deposit into c never fired")
        convs = [o for o, _ in ops if o.name == "fx.conversions.create"]
        if any(o.on_behalf_of != accts["a"] for o in convs):
            problems.append("a conversion ran outside employer a")

        # End state: every transfer, conversion and charge this run sent is terminal.
        objs: dict[str, list[str]] = {}
        for o, res in ops:
            if (
                o.name not in ("transfers.create", "fx.conversions.create", "charges.create")
                or not res.provider_id
            ):
                continue
            path = {
                "transfers.create": "/api/v1/transfers",
                "fx.conversions.create": "/api/v1/fx/conversions",
                "charges.create": "/api/v1/charges",
            }[o.name]
            obo = None if o.name == "charges.create" else o.on_behalf_of
            body = port.read(ReadRequest(f"{path}/{res.provider_id}", {}, on_behalf_of=obo))
            st = body.get("status")
            objs.setdefault(o.name, []).append(st)
            if st in IN_FLIGHT:
                problems.append(f"{o.name} left at {st}")
        if objs.get("transfers.create") and set(objs["transfers.create"]) != {"PAID"}:
            problems.append(f"transfers {objs['transfers.create']}")
        bens = sum(1 for o, _ in ops if o.name == "beneficiaries.create")
        made = {k: len(v) for k, v in objs.items()} | {"beneficiaries.create": bens}
        note(
            step="end_state",
            objects=made,
            statuses=objs,
            book=read_book(r.platform_store) is not None,
            sent_ops=[o.name for o, _ in ops],
        )
        outcome = "PASS" if not problems else "FAIL"
    except SystemExit as stop:
        problems.append(str(stop))
        outcome = "STOP"
    except Exception as exc:
        problems.append(f"{type(exc).__name__}: {exc}")
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()
        note(step="done", outcome=outcome, problems=problems, seconds=round(time.time() - t0, 1))
        if LIVE and RECORD:
            Path(RECORD).write_text(json.dumps(log, indent=1, default=str))
            Path(RECORD).chmod(0o600)
    return outcome, problems


def test_kit6_live_acceptance():
    outcome, problems = acceptance()
    assert outcome == "PASS", problems
