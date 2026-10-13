"""Offline tests for scripts/deployed_security_checks.py: its judgement, with no AWS call.

The script builds its boto3 clients in load_outputs, in the Region the outputs file names, and
talks to the Gateway only through `mcp`, which these tests replace.
"""

from __future__ import annotations

import importlib.util
import urllib.error
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "deployed_security_checks.py"


@pytest.fixture
def checks(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-southeast-2")
    spec = importlib.util.spec_from_file_location("deployed_security_checks", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pages(*pages):
    """A Gateway whose tools/list returns `pages` in order, keyed by the cursor it handed out."""
    by_cursor = {}
    for i, names in enumerate(pages):
        result = {"tools": [{"name": n} for n in names]}
        if i + 1 < len(pages):
            result["nextCursor"] = f"c{i + 1}"
        by_cursor[None if i == 0 else f"c{i}"] = result
    sent = []

    def mcp(body, bearer):
        assert body["method"] == "tools/list"
        cursor = body.get("params", {}).get("cursor")
        sent.append(cursor)
        return 200, {"jsonrpc": "2.0", "id": body["id"], "result": by_cursor[cursor]}

    return mcp, sent


def test_tools_list_follows_next_cursor_to_the_last_page(checks, monkeypatch):
    first = [f"k1___t{i}" for i in range(30)]
    second = [f"k5___t{i}" for i in range(30)]
    third = ["k8___read_seller"]
    mcp, sent = _pages(first, second, third)
    monkeypatch.setattr(checks, "mcp", mcp)
    code, names, pages, problem = checks.list_all_tools("tok")
    assert (code, len(names), pages, problem) == (200, 61, 3, None)
    assert names == first + second + third
    assert sent == [None, "c1", "c2"]


def test_tools_list_reports_a_repeated_cursor_as_a_problem(checks, monkeypatch):
    def mcp(body, bearer):
        return 200, {"result": {"tools": [{"name": "k1___x"}], "nextCursor": "same"}}

    monkeypatch.setattr(checks, "mcp", mcp)
    code, _names, pages, problem = checks.list_all_tools("tok")
    assert (code, pages) == (200, 2)
    assert problem == "page 2 repeats an earlier cursor"


def test_tools_list_reports_the_page_cap_as_a_problem(checks, monkeypatch):
    def mcp(body, bearer):
        n = body["id"]
        return 200, {"result": {"tools": [{"name": f"k1___t{n}"}], "nextCursor": f"c{n}"}}

    monkeypatch.setattr(checks, "mcp", mcp)
    monkeypatch.setattr(checks, "MAX_TOOL_PAGES", 3)
    code, names, pages, problem = checks.list_all_tools("tok")
    assert (code, len(names), pages) == (200, 3, 3)
    assert problem == "still paging after 3 pages"


def test_tools_list_reports_a_refused_page(checks, monkeypatch):
    monkeypatch.setattr(checks, "mcp", lambda body, bearer: (401, "unauthorized"))
    assert checks.list_all_tools(None) == (401, [], 1, "HTTP 401 on page 1")


@pytest.mark.parametrize(
    ("body", "problem"),
    [
        ({"result": {"tools": [{"description": "no name"}]}}, "has a tool entry with no name"),
        ({"result": {"tools": ["k1___x"]}}, "has a tool entry with no name"),
        ({"result": {"tools": [{"name": 7}]}}, "has a tool entry with no name"),
        ({"result": "not an object"}, "is not a tools/list result"),
        ({"result": {"tools": "k1___x"}}, "is not a tools/list result"),
        ({"error": {"code": -32603}}, "is not a tools/list result"),
        ("plain text", "is not a tools/list result"),
    ],
)
def test_tools_list_reports_a_malformed_page(checks, monkeypatch, body, problem):
    monkeypatch.setattr(checks, "mcp", lambda b, bearer: (200, body))
    code, _, _, found = checks.list_all_tools("tok")
    assert code == 200
    assert found == f"page 1 {problem}"


GATE = "kit k1 serves platform callers only; this caller's tenant is not 'platform'"


@pytest.mark.parametrize(
    ("out", "passed"),
    [
        ({"error": {"code": "TENANT_SCOPE", "message": GATE}}, True),
        # TENANT_SCOPE from the reader or later: the caller got past the tool plane's check.
        ({"error": {"code": "TENANT_SCOPE", "message": "the reader refused GET /x"}}, False),
        ({"error": {"code": "TENANT_SCOPE", "message": "tenant belongs to another kit"}}, False),
        ({"error": {"code": "TENANT_SCOPE"}}, False),
        ({"error": {"code": "TENANT_SCOPE", "message": ["serves platform callers only"]}}, False),
        # Got past the tenant check: an unseeded kit 1 answers NOT_CONFIGURED. A FAIL.
        ({"error": {"code": "NOT_CONFIGURED", "message": "kit 1 has no book"}}, False),
        ({"error": {"code": "INVALID_INPUT", "message": GATE}}, False),
        ({"error": {"code": "TENANT_SCOPE", "message": GATE}, "untrusted_data": {}}, False),
        ({"tool": "read_balances", "untrusted_data": {"balances": []}}, False),
        ({"_jsonrpc_error": {"code": -32603}}, False),
        ({"_raw": "gateway error", "_status": 502}, False),
        ({"error": "TENANT_SCOPE"}, False),
        ("TENANT_SCOPE", False),
        (None, False),
    ],
)
def test_a_cross_tenant_call_passes_only_on_the_tool_planes_refusal(checks, out, passed):
    assert checks.refused_by_tenant_check(out) is passed


@pytest.mark.parametrize(
    ("out", "passed"),
    [
        # A read answered: data, or a stated refusal inside the data before kit 1 is seeded.
        ({"tool": "read_balances", "untrusted_data": {"balances": []}}, True),
        ({"tool": "read_balances", "untrusted_data": {"status": "NOT_CONFIGURED"}}, True),
        # The reader could not complete the read, after the caller was verified.
        ({"error": {"code": "READ_FAILED", "message": "the reader refused GET /x"}}, True),
        # The caller's token never reached the tool, or the tool refused or failed.
        ({"error": {"code": "NOT_AUTHORISED", "message": "no caller token"}}, False),
        ({"error": {"code": "TENANT_SCOPE", "message": "serves platform callers only"}}, False),
        ({"error": {"code": "INTERNAL", "message": "the tool failed"}}, False),
        ({"untrusted_data": {}, "error": {"code": "INTERNAL"}}, False),
        ({"error": "READ_FAILED"}, False),
        ({"_jsonrpc_error": {"code": -32603}}, False),
        ({"_raw": "gateway error", "_status": 502}, False),
        ({"_result": None}, False),
        ({"_text": "[1]"}, False),
        (None, False),
    ],
)
def test_check_3_passes_only_on_evidence_the_tool_ran(checks, out, passed):
    assert checks.identity_reached_tool(out) is passed


@pytest.mark.parametrize(
    "body",
    [
        {"jsonrpc": "2.0", "id": 3, "result": None},
        {"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": "[1]"}]}},
        {"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": 7}]}},
        {"jsonrpc": "2.0", "id": 3, "result": {"content": "x"}},
    ],
)
def test_check_3_fails_a_malformed_reply(checks, body):
    assert checks.identity_reached_tool(checks.tool_payload((200, body))) is False


@pytest.mark.parametrize(
    "exc",
    [
        urllib.error.URLError("name resolution failed"),
        TimeoutError("timed out"),
        ConnectionResetError("reset"),
    ],
)
def test_mcp_turns_a_network_failure_into_a_reply_no_check_accepts(checks, monkeypatch, exc):
    def urlopen(*a, **k):
        raise exc

    monkeypatch.setattr(checks, "GW", "https://gateway.example.com/mcp")
    monkeypatch.setattr(checks.urllib.request, "urlopen", urlopen)
    code, body = checks.mcp({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, "tok")
    assert code == 0
    assert body.startswith("no reply: ")
    assert checks.refused_by_tenant_check(checks.tool_payload((code, body))) is False


@pytest.mark.parametrize(
    "body",
    [
        {"result": "not an object"},
        {"result": {"content": "not a list"}},
        {"result": {"content": ["not an object"]}},
        {"result": {"content": [{"type": "text", "text": 7}]}},
        {"result": {"content": [{"type": "text", "text": "[1, 2]"}]}},
        {"result": {"content": [{"type": "text", "text": "not json"}]}},
        {"error": "not an object"},
        "plain text",
    ],
)
def test_tool_payload_always_returns_an_object(checks, body):
    out = checks.tool_payload((200, body))
    assert isinstance(out, dict)
    assert checks.refused_by_tenant_check(out) is False


class _NoLambda:
    def invoke(self, **kw):
        raise RuntimeError("a direct invoke failed")


def test_a_broken_reply_fails_check_0_and_the_run_still_prints_its_summary(
    checks, monkeypatch, capsys
):
    """No AWS call: sign-in, stack lookup and Lambda are stubbed; the Gateway repeats a cursor
    and then a later step raises. Check 0 fails, the run stops with a FAIL, and the summary
    line is printed."""

    def mcp(body, bearer):
        if body["method"] == "tools/list" and bearer is None:
            return 401, "unauthorized"
        if body["method"] == "tools/list":
            return 200, {"result": {"tools": [{"name": "k1___x"}], "nextCursor": "same"}}
        return 200, {"result": {"content": [{"type": "text", "text": "{}"}]}}

    monkeypatch.setattr(checks, "token", lambda user: "a.b.c")
    monkeypatch.setattr(checks, "stack_resource", lambda *a: "fn")
    monkeypatch.setattr(checks, "mcp", mcp)
    monkeypatch.setattr(checks, "lam", _NoLambda())
    monkeypatch.setattr(checks.time, "sleep", lambda s: None)
    assert checks.main() == 1
    lines = capsys.readouterr().out.splitlines()
    assert any(
        ln.startswith("FAIL  0 authenticated tools/list") and "repeats an earlier cursor" in ln
        for ln in lines
    ), lines
    assert any(ln.startswith("FAIL  run stopped early  RuntimeError") for ln in lines), lines
    assert lines[-1].endswith(" passed")


def _outputs(tmp_path, agent):
    import json

    path = tmp_path / "cdk-outputs.json"
    path.write_text(
        json.dumps(
            {
                "AwxAgentStack": {
                    "AwxGatewayUrl": "https://gw",
                    "AwxUserPoolClientId": "c",
                    **agent,
                },
                "AwxSpine": {"AirwallexSecretArn": "arn:aws:secretsmanager:x:1:secret:s"},
            }
        )
    )
    return path


def test_clients_are_made_in_the_region_the_outputs_file_names(checks, tmp_path):
    checks.load_outputs(_outputs(tmp_path, {"AwxRegion": "eu-west-1"}))
    assert checks.REGION == "eu-west-1"
    for client in (checks.sm, checks.idp, checks.lam, checks.cfn, checks.logs):
        assert client.meta.region_name == "eu-west-1"


def test_an_outputs_file_without_a_region_uses_the_default_region(checks, tmp_path):
    from awx_common.regions import DEFAULT_REGION

    checks.load_outputs(_outputs(tmp_path, {}))
    assert checks.sm.meta.region_name == DEFAULT_REGION
