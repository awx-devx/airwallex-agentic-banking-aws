import io
import json
import time

import pytest
from awx_agent import cli

OUTPUTS = {
    "AwxAgentStack": {
        "AwxRegion": "ap-southeast-2",
        "AwxUserPoolClientId": "client123",
        "AwxRuntimeArn": (
            "arn:aws:bedrock-agentcore:ap-southeast-2:111111111111:runtime/awx_agent-abc"
        ),
        "AwxApprovalApiUrl": "https://api.example/",
    }
}
PID = "2b1f6c3e-8a51-4f3c-9d0e-0c4b8f1a2b3c"
HASH = "a" * 64


class FakeCognito:
    def __init__(self):
        self.calls = []

    def initiate_auth(self, **kw):
        self.calls.append(kw)
        return {
            "AuthenticationResult": {
                "AccessToken": "tok-1",
                "RefreshToken": "ref",
                "ExpiresIn": 3600,
            }
        }


@pytest.fixture
def env(tmp_path):
    cfg = tmp_path / "outputs.json"
    cfg.write_text(json.dumps(OUTPUTS))
    return {"AWXPOC_CONFIG": str(cfg), "AWXPOC_HOME": str(tmp_path / "home")}


def _run(argv, env, http=None, stdin="", cognito=None):
    out = io.StringIO()
    calls = []

    def fake_http(method, url, headers, body):
        calls.append((method, url, dict(headers), json.loads(body) if body else None))
        return http(method, url) if http else (200, b'{"reply": "hello"}')

    cog = cognito or FakeCognito()
    code = cli.main(
        argv,
        env=env,
        http=fake_http,
        cognito_factory=lambda r: cog,
        stdin=io.StringIO(stdin),
        stdout=out,
    )
    return code, out.getvalue(), calls, cog


def test_parser_shapes():
    p = cli.build_parser()
    a = p.parse_args(["chat", "--kit", "k3", "-m", "hi"])
    assert (a.command, a.kit, a.message) == ("chat", "k3", "hi")
    a = p.parse_args(["approve", PID, "--intent-hash", HASH])
    assert (a.command, a.proposal_id, a.intent_hash) == ("approve", PID, HASH)
    a = p.parse_args(["proposals", "list", "--status", "PENDING"])
    assert a.proposals_command == "list"
    for bad in (["chat", "--kit", "k9"], ["approve", PID], ["proposals"], []):
        with pytest.raises(SystemExit):
            p.parse_args(bad)


def test_login_stores_token_privately_and_never_prints_it(env, tmp_path):
    code, out, _, cog = _run(
        ["login", "--username", "requester1", "--password-stdin"], env, stdin="pw\n"
    )
    assert code == 0
    assert "tok-1" not in out
    assert cog.calls[0]["AuthParameters"] == {"USERNAME": "requester1", "PASSWORD": "pw"}
    path = tmp_path / "home" / "tokens-requester1.json"
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_chat_posts_to_runtime_with_bearer_and_session(env):
    _run(["login", "--username", "u", "--password-stdin"], env, stdin="pw\n")
    code, out, calls, _ = _run(["chat", "--kit", "k1", "-m", "show balances"], env)
    assert code == 0
    assert "hello" in out
    method, url, headers, body = calls[0]
    assert method == "POST"
    assert url.startswith(
        "https://bedrock-agentcore.ap-southeast-2.amazonaws.com/runtimes/arn%3Aaws%3A"
    )
    assert url.endswith("/invocations?qualifier=DEFAULT")
    assert headers["Authorization"] == "Bearer tok-1"
    assert len(headers["X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"]) >= 33
    assert body == {"kit": "k1", "prompt": "show balances"}


def test_approve_and_reject_send_bound_decision(env):
    _run(["login", "--username", "approver1", "--password-stdin"], env, stdin="pw\n")
    for verb in ("approve", "reject"):
        code, _, calls, _ = _run(
            [verb, PID, "--intent-hash", HASH], env, http=lambda m, u: (200, b'{"ok": true}')
        )
        assert code == 0
        assert calls[0][:2] == ("POST", f"https://api.example/proposals/{PID}/decision")
        assert calls[0][3] == {"decision": verb.upper(), "intent_hash": HASH}


def test_proposals_list(env):
    _run(["login", "--username", "u", "--password-stdin"], env, stdin="pw\n")
    code, _out, calls, _ = _run(
        ["proposals", "list", "--status", "PENDING"], env, http=lambda m, u: (200, b"[]")
    )
    assert code == 0
    assert calls[0][1] == "https://api.example/proposals?status=PENDING"


@pytest.mark.parametrize(
    "argv",
    [
        ["approve", "not-a-uuid", "--intent-hash", HASH],
        ["approve", PID, "--intent-hash", "XYZ"],
        ["chat", "--kit", "k1", "--session", "short", "-m", "x"],
    ],
)
def test_argument_validation(env, argv):
    code, _, calls, _ = _run(argv, env)
    assert code == 2
    assert calls == []


def test_requires_login_and_config(env, tmp_path):
    assert _run(["proposals", "list"], env)[0] == 2
    assert _run(["proposals", "list"], {"AWXPOC_HOME": str(tmp_path)})[0] == 2


def test_expired_token_refreshes(env, tmp_path):
    cli.save_tokens(
        env,
        "u",
        {"access_token": "old", "refresh_token": "ref", "expires_at": int(time.time()) - 5},
    )
    _, _, calls, cog = _run(["proposals", "list"], env, http=lambda m, u: (200, b"[]"))
    assert cog.calls[0]["AuthFlow"] == "REFRESH_TOKEN_AUTH"
    assert calls[0][2]["Authorization"] == "Bearer tok-1"


def test_http_errors_surface(env):
    _run(["login", "--username", "u", "--password-stdin"], env, stdin="pw\n")
    assert (
        _run(
            ["approve", PID, "--intent-hash", HASH],
            env,
            http=lambda m, u: (409, b'{"error":"hash"}'),
        )[0]
        == 2
    )


def test_approval_api_410_and_202_show_the_apis_own_text(env, capsys):
    """The approval API returns 410 after a failed task-token send and 202 for a recorded,
    still-pending decision; the CLI prints each body as the API wrote it."""
    _run(["login", "--username", "approver1", "--password-stdin"], env, stdin="pw\n")
    gone = b'{"error": "the execution is no longer waiting for this decision"}'
    code, _, _, _ = _run(
        ["approve", PID, "--intent-hash", HASH], env, http=lambda m, u: (410, gone)
    )
    assert code == 2
    assert "the execution is no longer waiting for this decision" in capsys.readouterr().err
    pending = b'{"state": "PENDING", "recorded": true}'
    code, out, _, _ = _run(
        ["approve", PID, "--intent-hash", HASH], env, http=lambda m, u: (202, pending)
    )
    assert code == 0
    assert '"state": "PENDING"' in out


@pytest.mark.parametrize(
    ("body", "model"),
    [
        (b'{"kit": "k1", "model": "us.anthropic.claude-sonnet-5", "reply": "hello"}', None),
        (b'{"reply": "hello"}', "an unknown model"),
    ],
    ids=["model-named", "model-missing"],
)
def test_chat_prints_the_answering_model_to_stderr(env, body, model):
    _run(["login", "--username", "u", "--password-stdin"], env, stdin="pw\n")
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(
        ["chat", "--kit", "k1", "-m", "show balances"],
        env=env,
        http=lambda method, url, headers, data: (200, body),
        cognito_factory=lambda r: FakeCognito(),
        stdin=io.StringIO(""),
        stdout=out,
        stderr=err,
    )
    assert code == 0
    named = model or json.loads(body)["model"]
    assert err.getvalue() == f"(answered by {named})\n"
    # stdout carries the session line and the reply, and never the model.
    assert out.getvalue().splitlines()[1:] == ["hello"]
    assert "answered by" not in out.getvalue()
