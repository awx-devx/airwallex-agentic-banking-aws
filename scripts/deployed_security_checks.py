"""Security checks against a deployed stack set (read-only; no money-moving tool is called).

Run from the repo root with AWS credentials for the deploy account, after `make deploy` has
written the stack outputs to infra/cdk-outputs.json:
    AWS_PROFILE=awx-deploy uv run --all-packages python scripts/deployed_security_checks.py
    ... scripts/deployed_security_checks.py --outputs path/to/cdk-outputs.json

The checks run in the Region the stacks were deployed to (the AwxRegion output of
AwxAgentStack), falling back to awx_common.regions.DEFAULT_REGION for an outputs file without
it. Prints one PASS/FAIL line per check and no secret, token or password.

Signs in as the demo users `demo-platform-requester`, `demo-k6-operator` and
`demo-k5-operator-approver` (docs/deployment.md, "Demo users").
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import boto3
from awx_common.regions import DEFAULT_REGION

REGION = ""  # the deployment's Region: AwxAgentStack.AwxRegion in the outputs file
# The file `cdk deploy --outputs-file cdk-outputs.json` writes when run from infra/.
DEFAULT_OUTPUTS = Path(__file__).resolve().parents[1] / "infra" / "cdk-outputs.json"
OUT: dict = {}
GW = ""
CLIENT = ""
SECRET_ARN = ""


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--outputs",
        type=Path,
        default=DEFAULT_OUTPUTS,
        help="stack outputs JSON written by cdk deploy --outputs-file "
        "(default: infra/cdk-outputs.json)",
    )
    return parser.parse_args(argv)


def load_outputs(path: Path) -> None:
    """Read the deployed stack outputs the checks need."""
    global OUT, GW, CLIENT, SECRET_ARN, REGION
    OUT = json.loads(path.read_text())
    GW = OUT["AwxAgentStack"]["AwxGatewayUrl"]
    CLIENT = OUT["AwxAgentStack"]["AwxUserPoolClientId"]
    SECRET_ARN = OUT["AwxSpine"]["AirwallexSecretArn"]
    REGION = OUT["AwxAgentStack"].get("AwxRegion") or DEFAULT_REGION
    make_clients(REGION)


# The AWS clients, made by make_clients once the Region is known from the outputs file.
sm: Any = None
idp: Any = None
lam: Any = None
iam: Any = None
cfn: Any = None
logs: Any = None


def make_clients(region: str) -> None:
    """Create the AWS clients in the deployment's Region."""
    global sm, idp, lam, iam, cfn, logs
    sm = boto3.client("secretsmanager", region_name=region)
    idp = boto3.client("cognito-idp", region_name=region)
    lam = boto3.client("lambda", region_name=region)
    iam = boto3.client("iam")
    cfn = boto3.client("cloudformation", region_name=region)
    logs = boto3.client("logs", region_name=region)


results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)


def token(user: str) -> str:
    cred = json.loads(sm.get_secret_value(SecretId=f"awx-poc/demo-users/{user}")["SecretString"])
    r = idp.initiate_auth(
        ClientId=CLIENT,
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": cred["username"], "PASSWORD": cred["password"]},
    )
    return r["AuthenticationResult"]["AccessToken"]


def mcp(body: dict, bearer: str | None) -> tuple[int, dict | str]:
    req = urllib.request.Request(  # noqa: S310 - fixed https URLs from the stack outputs
        GW,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **({"Authorization": f"Bearer {bearer}"} if bearer else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:  # noqa: S310 - fixed https URL
            text = resp.read().decode()
            code = resp.status
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:300]
    except (urllib.error.URLError, TimeoutError, OSError) as e:  # no HTTP reply at all
        return 0, f"no reply: {type(e).__name__}"
    if text.lstrip().startswith("event:") or "\ndata:" in text or text.startswith("data:"):
        datas = [ln[5:].strip() for ln in text.splitlines() if ln.startswith("data:")]
        text = datas[-1] if datas else text
    try:
        return code, json.loads(text)
    except ValueError:
        return code, text[:300]


def call(tool: str, args: dict, bearer: str | None, rid: int = 1):
    return mcp(
        {
            "jsonrpc": "2.0",
            "id": rid,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        },
        bearer,
    )


def tool_payload(resp) -> dict:
    """The tool Lambda's JSON result inside an MCP tools/call response, always a dict.

    A reply of any other shape comes back as a dict that no check accepts (`_raw`,
    `_jsonrpc_error`, `_text`, `_result`), so a malformed reply is a FAIL, never a crash.
    """
    code, body = resp
    if not isinstance(body, dict):
        return {"_raw": body, "_status": code}
    if "error" in body:
        return {"_jsonrpc_error": body["error"]}
    result = body.get("result")
    content = result.get("content") if isinstance(result, dict) else None
    for c in content if isinstance(content, list) else []:
        if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str):
            try:
                out = json.loads(c["text"])
            except ValueError:
                return {"_text": c["text"][:300]}
            return out if isinstance(out, dict) else {"_text": c["text"][:300]}
    return {"_result": result}


def error_of(out) -> dict:
    """The tool's {"error": {...}} object, or {} when there is none or it is not an object."""
    err = out.get("error") if isinstance(out, dict) else None
    return err if isinstance(err, dict) else {}


def identity_reached_tool(out) -> bool:
    """Positive evidence for check 3 that the verified caller reached the kit 1 tool code.

    A read answers with `untrusted_data` (data, or a stated refusal such as NOT_CONFIGURED
    before kit 1 is seeded), and a read the reader could not complete answers READ_FAILED. The
    tool returns either only after it has verified the caller's token. Any other reply,
    including a malformed one, is a FAIL.
    """
    if not isinstance(out, dict) or "_jsonrpc_error" in out:
        return False
    if "untrusted_data" in out:
        return "error" not in out
    return error_of(out).get("code") == "READ_FAILED"


MAX_TOOL_PAGES = 50  # paging that reaches this cap fails check 0 instead of hanging


def list_all_tools(bearer: str | None) -> tuple[int, list[str], int, str | None]:
    """Every tool name from the Gateway's paginated tools/list, following nextCursor.

    Returns (HTTP status of the last page, names, pages read, problem). `problem` is None when
    the last page had no nextCursor. Otherwise it says why the walk stopped early: a non-200
    page, a reply that is not a JSON-RPC result object, a tool entry with no name, a repeated
    cursor or the page cap. Check 0 fails on any problem.
    """
    names: list[str] = []
    cursor: str | None = None
    seen: set[str] = set()
    code = 0
    for page in range(1, MAX_TOOL_PAGES + 1):
        body_in: dict = {"jsonrpc": "2.0", "id": 100 + page, "method": "tools/list"}
        if cursor is not None:
            body_in["params"] = {"cursor": cursor}
        code, body = mcp(body_in, bearer)
        if code != 200:
            return code, names, page, f"HTTP {code} on page {page}"
        result = body.get("result") if isinstance(body, dict) else None
        tools = result.get("tools", []) if isinstance(result, dict) else None
        if not isinstance(tools, list):
            return code, names, page, f"page {page} is not a tools/list result"
        for t in tools:
            name = t.get("name") if isinstance(t, dict) else None
            if not isinstance(name, str) or not name:
                return code, names, page, f"page {page} has a tool entry with no name"
            names.append(name)
        cursor = result.get("nextCursor")
        if not cursor:
            return code, names, page, None
        if cursor in seen:
            return code, names, page, f"page {page} repeats an earlier cursor"
        seen.add(cursor)
    return code, names, MAX_TOOL_PAGES, f"still paging after {MAX_TOOL_PAGES} pages"


# The tool plane's own refusal of a kit 1-4 caller that is not the platform
# (awx_agent.identity.CallerIdentity.run_tenant).
PLATFORM_ONLY_REFUSAL = "serves platform callers only"


def refused_by_tenant_check(out) -> bool:
    """A cross-tenant tool call passes only when the tool plane's tenant check refused it.

    That check runs before the tool reads its arguments or any kit state, and its refusal is
    TENANT_SCOPE with a message containing PLATFORM_ONLY_REFUSAL. Any other outcome means the
    caller got past it or it never ran, and counts as a FAIL whether or not the kit was seeded:
    data, NOT_CONFIGURED, INVALID_INPUT, a JSON-RPC error, a malformed reply, or a TENANT_SCOPE
    raised later (by the reader, for example).
    """
    if not isinstance(out, dict) or "untrusted_data" in out:
        return False
    err = error_of(out)
    message = err.get("message")
    return (
        err.get("code") == "TENANT_SCOPE"
        and isinstance(message, str)
        and PLATFORM_ONLY_REFUSAL in message
    )


def stack_resource(stack: str, prefix: str, rtype: str) -> str:
    for page in cfn.get_paginator("list_stack_resources").paginate(StackName=stack):
        for r in page["StackResourceSummaries"]:
            if r["LogicalResourceId"].startswith(prefix) and r["ResourceType"] == rtype:
                return r["PhysicalResourceId"]
    raise LookupError(f"{stack}/{prefix}")


def forge(tok: str) -> str:
    """Same header and claims, signature from 'another key' (random bytes)."""
    head, payload, _ = tok.split(".")
    import os

    return f"{head}.{payload}." + base64.urlsafe_b64encode(os.urandom(256)).rstrip(b"=").decode()


def main() -> int:
    """Run every check and print the summary line, even when a reply breaks a check."""
    try:
        run_checks()
    except Exception as e:  # a malformed reply or a failed AWS call: FAIL, and still summarise
        check("run stopped early", False, f"{type(e).__name__}; later checks did not run")
    failed = [n for n, ok, _ in results if not ok]
    print(f"{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


def run_checks() -> None:
    req_tok = token("demo-platform-requester")
    k6_tok = token("demo-k6-operator")
    k1_fn = stack_resource("AwxAgentStack", "K1ToolsHandler", "AWS::Lambda::Function")

    # 1. Unauthenticated Gateway call is refused.
    code, body = mcp({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, None)
    check("1 unauthenticated tools/list refused", code in (401, 403), f"HTTP {code}")

    # tools/list with a token, every page: tools carry the kit prefix.
    code, names, pages, problem = list_all_tools(req_tok)
    check(
        "0 authenticated tools/list",
        code == 200 and problem is None and any(n.startswith("k1___") for n in names),
        f"HTTP {code}, {len(names)} tools across {pages} pages"
        + (f", {problem}" if problem else ""),
    )

    # 2. Bad signature (Gateway behaviour A: authorizer and/or interceptor reject; tool Lambda
    # never invoked).
    t0 = int(time.time() * 1000)
    code, body = call("k1___read_balances", {}, forge(req_tok))
    out = tool_payload((code, body))
    refused = (
        code in (401, 403)
        or "_jsonrpc_error" in out
        or (error_of(out).get("code") == "NOT_AUTHORISED")
    )
    check("2 forged-signature token refused", refused, f"HTTP {code}")
    t1 = int(time.time() * 1000)
    time.sleep(3)  # the next call's START must fall outside [t0, t1 + 1 s]

    # 3. Identity reaches the Lambda (Gateway behaviour B: _awx_caller passes through).
    code, body = call("k1___read_balances", {}, req_tok, rid=3)
    out = tool_payload((code, body))
    err = error_of(out)
    check(
        "3 identity reaches the tool Lambda (_awx_caller passed through)",
        code == 200 and identity_reached_tool(out),
        f"HTTP {code}, result keys {sorted(out)[:5]}, error {err.get('code')}",
    )

    # 4. Another kit's tool is refused by the kit 1 tool Lambda (direct invoke, the strongest form:
    # a valid token, a k2 tool name).
    def direct(fn, tool, caller):
        ctx = base64.b64encode(json.dumps({"custom": {"bedrockAgentCoreToolName": tool}}).encode())
        r = lam.invoke(
            FunctionName=fn,
            ClientContext=ctx.decode(),
            Payload=json.dumps({"_awx_caller": caller}).encode(),
        )
        try:
            out = json.loads(r["Payload"].read() or b"{}")
        except ValueError:
            return {"_raw": "unparseable Lambda payload"}
        return out if isinstance(out, dict) else {"_raw": out}

    out = direct(k1_fn, "k2___read_card_controls", req_tok)
    check(
        "4 another kit's tool refused by the kit 1 tool Lambda",
        "error" in out and "untrusted_data" not in out,
        f"error {error_of(out).get('code')}",
    )

    # 3b. Direct invoke with a forged caller is useless.
    out = direct(k1_fn, "k1___read_balances", forge(req_tok))
    check(
        "3b direct invoke with a forged _awx_caller refused",
        error_of(out).get("code") == "NOT_AUTHORISED",
        f"error {error_of(out).get('code')}",
    )

    # 5. Cross-tenant: kits 1-4 serve platform callers only. A k6 operator (a tenant with a
    # row in another kit) is refused by the tenant check, whether or not kit 1 is seeded.
    code, body = call("k1___read_balances", {}, k6_tok, rid=5)
    out = tool_payload((code, body))
    check(
        "5 k6:operator caller cannot read kit 1 (platform) data",
        refused_by_tenant_check(out),
        f"HTTP {code}, error {error_of(out).get('code')}",
    )
    # 5c. A tenant with no tenants-table row (k5:operator) is refused too, never the platform.
    try:
        k5_op_tok = token("demo-k5-operator-approver")
    except Exception as e:  # no such demo user: the check could not run, so it does not pass
        check(
            "5c k5:operator caller (no tenants row) cannot read kit 1 (platform) data",
            False,
            f"cannot sign in as demo-k5-operator-approver: {type(e).__name__}",
        )
    else:
        code, body = call("k1___read_balances", {}, k5_op_tok, rid=6)
        out = tool_payload((code, body))
        check(
            "5c k5:operator caller (no tenants row) cannot read kit 1 (platform) data",
            refused_by_tenant_check(out),
            f"HTTP {code}, error {error_of(out).get('code')}",
        )
    # 5b. Approval API: the k6 operator sees no platform proposals.
    api = OUT["AwxSpine"]["ApprovalApiUrl"].rstrip("/") + "/proposals"
    req = urllib.request.Request(  # noqa: S310
        api, headers={"Authorization": f"Bearer {k6_tok}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            items = json.loads(resp.read().decode())
        if isinstance(items, dict):
            rows = items.get("proposals")
        else:
            rows = items
        if not isinstance(rows, list):
            raise ValueError("not a proposals list")
        leaked = [
            p
            for p in rows
            if not isinstance(p, dict) or p.get("tenant_id") not in (None, "k6:operator")
        ]
        check("5b approval API lists no other tenant's proposals", not leaked, f"{len(rows)} rows")
    except urllib.error.HTTPError as e:
        check(
            "5b approval API lists no other tenant's proposals",
            e.code in (401, 403, 404),
            f"HTTP {e.code}",
        )
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        check(
            "5b approval API lists no other tenant's proposals",
            False,
            f"no usable reply: {type(e).__name__}",
        )

    # 6. The reader refuses a kit named in the payload.
    from awx_common.ops import ReadRequest
    from awx_common.transport import encode

    reader = stack_resource("AwxSpine", "ReaderK1Fn", "AWS::Lambda::Function")
    payload = encode(
        {"kit": "k2", "read": ReadRequest(path="/api/v1/balances/current"), "tenant_id": None}
    )
    r = lam.invoke(FunctionName=reader, Payload=json.dumps(payload).encode())  # encode -> JSON-able
    out = r["Payload"].read().decode()[:400]
    check(
        "6 ReaderK1 refuses a payload naming k2",
        "FunctionError" in r or "refus" in out.lower() or "error" in out.lower(),
        out[:120].replace("\n", " "),
    )

    # 7. Step roles cannot read the Airwallex secret.
    roles = []
    for k in range(1, 9):
        for page in cfn.get_paginator("list_stack_resources").paginate(StackName=f"AwxKitK{k}"):
            roles += [
                r["PhysicalResourceId"]
                for r in page["StackResourceSummaries"]
                if r["ResourceType"] == "AWS::IAM::Role" and "Step" in r["LogicalResourceId"]
            ]
    acct = boto3.client("sts").get_caller_identity()["Account"]
    allowed = []
    for name in roles:
        sim = iam.simulate_principal_policy(
            PolicySourceArn=f"arn:aws:iam::{acct}:role/{name}",
            ActionNames=["secretsmanager:GetSecretValue"],
            ResourceArns=[SECRET_ARN],
        )
        if any(e["EvalDecision"] == "allowed" for e in sim["EvaluationResults"]):
            allowed.append(name)
    check(
        "7 no step role can read the Airwallex secret",
        bool(roles) and not allowed,
        f"{len(roles)} step roles simulated, {len(allowed)} allowed",
    )

    # Did the forged call reach the kit 1 tool Lambda? (behaviour A, logs)
    group = f"/aws/lambda/{k1_fn}"
    try:
        lg = (
            lam.get_function_configuration(FunctionName=k1_fn)
            .get("LoggingConfig", {})
            .get("LogGroup")
        )
        group = lg or group
        time.sleep(10)
        ev = logs.filter_log_events(logGroupName=group, startTime=t0, endTime=t1 + 1000).get(
            "events", []
        )
        check(
            "2b forged token: tool Lambda not invoked during the forged call",
            not any("START RequestId" in e["message"] for e in ev),
            f"{len(ev)} log events",
        )
    except Exception as e:
        check(
            "2b forged token: tool Lambda not invoked",
            False,
            f"log read failed: {type(e).__name__}",
        )


if __name__ == "__main__":
    load_outputs(parse_args(sys.argv[1:]).outputs)
    sys.exit(main())
