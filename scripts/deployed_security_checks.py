"""Security checks against a deployed stack set (read-only; no money-moving tool is called).

Run from the repo root with AWS credentials for the deploy account, after `make deploy` has
written the stack outputs to infra/cdk-outputs.json:
    AWS_PROFILE=awx-deploy uv run --all-packages python scripts/deployed_security_checks.py
    ... scripts/deployed_security_checks.py --outputs path/to/cdk-outputs.json

Prints one PASS/FAIL line per check and no secret, token or password.
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

import boto3

REGION = "ap-southeast-2"
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
    global OUT, GW, CLIENT, SECRET_ARN
    OUT = json.loads(path.read_text())
    GW = OUT["AwxAgentStack"]["AwxGatewayUrl"]
    CLIENT = OUT["AwxAgentStack"]["AwxUserPoolClientId"]
    SECRET_ARN = OUT["AwxSpine"]["AirwallexSecretArn"]


sm = boto3.client("secretsmanager", region_name=REGION)
idp = boto3.client("cognito-idp", region_name=REGION)
lam = boto3.client("lambda", region_name=REGION)
iam = boto3.client("iam")
cfn = boto3.client("cloudformation", region_name=REGION)
logs = boto3.client("logs", region_name=REGION)

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
        return e.code, e.read().decode()[:300]
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
    """The tool Lambda's JSON result inside an MCP tools/call response."""
    code, body = resp
    if not isinstance(body, dict):
        return {"_raw": body, "_status": code}
    if "error" in body:
        return {"_jsonrpc_error": body["error"]}
    content = body.get("result", {}).get("content", [])
    for c in content:
        if c.get("type") == "text":
            try:
                return json.loads(c["text"])
            except ValueError:
                return {"_text": c["text"][:300]}
    return {"_result": body.get("result")}


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
    req_tok = token("demo-platform-requester")
    k6_tok = token("demo-k6-operator")
    k1_fn = stack_resource("AwxAgentStack", "K1ToolsHandler", "AWS::Lambda::Function")

    # 1. Unauthenticated Gateway call is refused.
    code, body = mcp({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, None)
    check("1 unauthenticated tools/list refused", code in (401, 403), f"HTTP {code}")

    # tools/list with a token: tools carry the kit prefix.
    code, body = mcp({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, req_tok)
    names = (
        [t["name"] for t in body.get("result", {}).get("tools", [])]
        if isinstance(body, dict)
        else []
    )
    check(
        "0 authenticated tools/list",
        code == 200 and any(n.startswith("k1___") for n in names),
        f"HTTP {code}, {len(names)} tools",
    )

    # 2. Bad signature (Gateway behaviour A: authorizer and/or interceptor reject; tool Lambda
    # never invoked).
    t0 = int(time.time() * 1000)
    code, body = call("k1___read_balances", {}, forge(req_tok))
    out = tool_payload((code, body))
    refused = (
        code in (401, 403)
        or "_jsonrpc_error" in out
        or (out.get("error", {}).get("code") == "NOT_AUTHORISED")
    )
    check("2 forged-signature token refused", refused, f"HTTP {code}")
    t1 = int(time.time() * 1000)
    time.sleep(3)  # the next call's START must fall outside [t0, t1 + 1 s]

    # 3. Identity reaches the Lambda (Gateway behaviour B: _awx_caller passes through).
    code, body = call("k1___read_balances", {}, req_tok, rid=3)
    out = tool_payload((code, body))
    err = out.get("error", {})
    no_caller = (
        "caller" in str(err.get("message", "")).lower() and err.get("code") == "NOT_AUTHORISED"
    )
    check(
        "3 identity reaches the tool Lambda (_awx_caller passed through)",
        code == 200 and not no_caller and "_jsonrpc_error" not in out,
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
        return json.loads(r["Payload"].read() or b"{}")

    out = direct(k1_fn, "k2___read_card_controls", req_tok)
    check(
        "4 another kit's tool refused by the kit 1 tool Lambda",
        "error" in out and "untrusted_data" not in out,
        f"error {out.get('error', {}).get('code')}",
    )

    # 3b. Direct invoke with a forged caller is useless.
    out = direct(k1_fn, "k1___read_balances", forge(req_tok))
    check(
        "3b direct invoke with a forged _awx_caller refused",
        out.get("error", {}).get("code") == "NOT_AUTHORISED",
        f"error {out.get('error', {}).get('code')}",
    )

    # 5. Cross-tenant: a k6 operator cannot use kit 1 (platform tenant) data.
    code, body = call("k1___read_balances", {}, k6_tok, rid=5)
    out = tool_payload((code, body))
    check(
        "5 k6:operator caller cannot read kit 1 (platform) data",
        "untrusted_data" not in out and ("error" in out or "_jsonrpc_error" in out),
        f"HTTP {code}, error {out.get('error', {}).get('code')}",
    )
    # 5b. Approval API: the k6 operator sees no platform proposals.
    api = OUT["AwxSpine"]["ApprovalApiUrl"].rstrip("/") + "/proposals"
    req = urllib.request.Request(  # noqa: S310
        api, headers={"Authorization": f"Bearer {k6_tok}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            items = json.loads(resp.read().decode())
        rows = items.get("proposals", items if isinstance(items, list) else [])
        leaked = [
            p
            for p in rows
            if isinstance(p, dict) and p.get("tenant_id") not in (None, "k6:operator")
        ]
        check("5b approval API lists no other tenant's proposals", not leaked, f"{len(rows)} rows")
    except urllib.error.HTTPError as e:
        check(
            "5b approval API lists no other tenant's proposals",
            e.code in (401, 403, 404),
            f"HTTP {e.code}",
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

    failed = [n for n, ok, _ in results if not ok]
    print(f"{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    load_outputs(parse_args(sys.argv[1:]).outputs)
    sys.exit(main())
