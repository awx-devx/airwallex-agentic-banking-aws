import base64
import json
from types import SimpleNamespace

import pytest
from awx_agent.runtime import app as rt
from awx_common.regions import DEFAULT_REGION

ENV = {"AWX_GATEWAY_URL": "https://gw.example/mcp", "AWX_REGION": DEFAULT_REGION}


def _token(sub="user-1"):
    seg = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"Bearer {seg({'alg': 'RS256'})}.{seg({'sub': sub})}.c2ln"


class FakeMcp:
    def __init__(self, url, token, kit):
        self.args = (url, token, kit)
        self.entered = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *exc):
        return False

    def list_tools_sync(self):
        return ["k1___read_balances"]


class ModelError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


def _agent_factory(log, fail_models=(), use_tool_then_fail=False):
    def make(model_id, region, kit, tools, messages):
        def call(prompt):
            log.append(model_id)
            if use_tool_then_fail:
                agent.messages.append({"role": "assistant", "content": [{"toolUse": {}}]})
            if model_id in fail_models:
                raise ModelError("AccessDeniedException")
            agent.messages.append({"role": "assistant", "content": [{"text": "ok"}]})
            return "ok"

        agent = SimpleNamespace(messages=list(messages))
        agent.__call__ = call
        return _Callable(agent, call)

    return make


class _Callable:
    def __init__(self, ns, fn):
        self.ns, self.fn = ns, fn

    @property
    def messages(self):
        return self.ns.messages

    def __call__(self, prompt):
        return self.fn(prompt)


@pytest.fixture(autouse=True)
def _fresh_sessions(monkeypatch):
    monkeypatch.setattr(rt, "SESSIONS", rt._Sessions())


def test_turn_uses_user_token_for_gateway_and_primary_model():
    seen = {}

    def mcp(url, token, kit):
        seen["mcp"] = FakeMcp(url, token, kit)
        return seen["mcp"]

    log = []
    out = rt.run_turn(
        kit="k1",
        prompt="hi",
        token=_token(),
        session_id="s" * 33,
        env=ENV,
        mcp_factory=mcp,
        agent_factory=_agent_factory(log),
    )
    assert out == {"kit": "k1", "model": rt.DEFAULT_MODEL_ID, "reply": "ok"}
    assert seen["mcp"].args == (ENV["AWX_GATEWAY_URL"], _token(), "k1")
    assert seen["mcp"].entered


def test_falls_back_when_primary_model_is_unavailable_before_any_tool_ran():
    log = []
    out = rt.run_turn(
        kit="k1",
        prompt="hi",
        token=_token(),
        session_id="s",
        env=ENV,
        mcp_factory=FakeMcp,
        agent_factory=_agent_factory(log, fail_models={rt.DEFAULT_MODEL_ID}),
    )
    assert log == [rt.DEFAULT_MODEL_ID, rt.DEFAULT_FALLBACK_MODEL_ID]
    assert out["model"] == rt.DEFAULT_FALLBACK_MODEL_ID


def test_the_default_agent_sends_no_sampling_parameters():
    # Claude Sonnet 5 rejects `temperature` with a ValidationException.
    agent = rt._default_agent(rt.DEFAULT_MODEL_ID, DEFAULT_REGION, "k1", [], [])
    config = agent.model.get_config()
    assert config["model_id"] == rt.DEFAULT_MODEL_ID
    assert not {"temperature", "top_p", "top_k"} & set(config)
    # Check the request Strands builds, not only the config. BedrockModel.format_request is a
    # Strands internal outside its documented API, pinned by uv.lock (strands-agents 1.58.0),
    # so a Strands upgrade that changes the request shape fails this test.
    request = agent.model.format_request([{"role": "user", "content": [{"text": "hi"}]}])
    assert request["modelId"] == rt.DEFAULT_MODEL_ID
    assert request["inferenceConfig"] == {}
    assert "additionalModelRequestFields" not in request


class RejectedRequest(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.response = {"Error": {"Code": code, "Message": message}}


def test_the_fallback_warning_carries_the_error_message(caplog):
    message = "ValidationException: temperature is deprecated for this model. " + "x" * 500

    def make(model_id, region, kit, tools, messages):
        def call(prompt):
            if model_id == rt.DEFAULT_MODEL_ID:
                raise RejectedRequest("ValidationException", message)
            return "ok"

        return _Callable(SimpleNamespace(messages=list(messages)), call)

    with caplog.at_level("WARNING", logger=rt.log.name):
        out = rt.run_turn(
            kit="k1",
            prompt="hi",
            token=_token(),
            session_id="s",
            env=ENV,
            mcp_factory=FakeMcp,
            agent_factory=make,
        )
    assert out["model"] == rt.DEFAULT_FALLBACK_MODEL_ID
    (record,) = [r for r in caplog.records if "trying fallback" in r.getMessage()]
    text = record.getMessage()
    assert "failed (ValidationException" in text
    assert "temperature is deprecated for this model" in text
    assert "unavailable" not in text
    assert len(text) < len(message)


def test_no_fallback_after_a_tool_ran():
    with pytest.raises(ModelError):
        rt.run_turn(
            kit="k1",
            prompt="hi",
            token=_token(),
            session_id="s",
            env=ENV,
            mcp_factory=FakeMcp,
            agent_factory=_agent_factory(
                [], fail_models={rt.DEFAULT_MODEL_ID}, use_tool_then_fail=True
            ),
        )


def test_history_is_per_user():
    log = []
    kw = dict(
        kit="k1",
        prompt="hi",
        session_id="s",
        env=ENV,
        mcp_factory=FakeMcp,
        agent_factory=_agent_factory(log),
    )
    rt.run_turn(token=_token("a"), **kw)
    rt.run_turn(token=_token("a"), **kw)
    rt.run_turn(token=_token("b"), **kw)
    assert len(rt.SESSIONS.get(("s", "k1", "a"))) == 2
    assert len(rt.SESSIONS.get(("s", "k1", "b"))) == 1


def test_entrypoint_rejects_bad_payloads_and_missing_token():
    ctx = SimpleNamespace(request_headers={"Authorization": _token()}, session_id="s")
    assert rt.invoke({"kit": "k9", "prompt": "x"}, ctx)["error"]["code"] == "BAD_REQUEST"
    assert rt.invoke({"kit": "k1", "prompt": " "}, ctx)["error"]["code"] == "BAD_REQUEST"
    assert (
        rt.invoke({"kit": "k1", "prompt": "x"}, SimpleNamespace(request_headers={}))["error"][
            "code"
        ]
        == "BAD_REQUEST"
    )


def test_tool_filter_admits_only_this_kits_read_and_propose_tools():
    (pattern,) = rt.kit_tool_filter("k2")["allowed"]
    assert pattern.match("k2___read_card")
    assert pattern.match("k2___propose_card_controls")
    assert not pattern.match("k1___read_card")
    assert not pattern.match("k2___delete_card")


# --- every page of the Gateway's tool list; refuse a turn with no tools --------------------


class _Page(list):
    def __init__(self, items, token=None):
        super().__init__(items)
        self.pagination_token = token


class PagedMcp(FakeMcp):
    """Gateway-like paging: the filtered tools of page n, then a token for page n+1."""

    def __init__(self, pages):
        super().__init__("u", "t", "k")
        self.pages = pages
        self.calls = []

    def list_tools_sync(self, pagination_token=None):
        self.calls.append(pagination_token)
        i = 0 if pagination_token is None else int(pagination_token)
        nxt = str(i + 1) if i + 1 < len(self.pages) else None
        return _Page(self.pages[i], nxt)


def _run(mcp, log, kit="k7"):
    return rt.run_turn(
        kit=kit,
        prompt="hi",
        token=_token(),
        session_id="s" * 40,
        env=ENV,
        mcp_factory=lambda *a: mcp,
        agent_factory=_tools_agent(log),
    )


def _tools_agent(log):
    def make(model_id, region, kit, tools, messages):
        log.append(list(tools))
        return _Callable(SimpleNamespace(messages=list(messages)), lambda p: "ok")

    return make


@pytest.mark.parametrize(
    "pages",
    [
        [[], ["k7___read_loan", "k7___propose_disburse_advance"]],  # kit on page 2 (live shape)
        [[], ["k7___read_loan"], ["k7___propose_collect_weekly_repayment"]],  # three pages
    ],
    ids=["two-pages", "three-pages"],
)
def test_tools_on_later_pages_reach_the_model(pages):
    mcp, log = PagedMcp(pages), []
    out = _run(mcp, log)
    assert out["reply"] == "ok"
    assert log == [[t for page in pages for t in page]]
    assert mcp.calls == [None, *[str(i) for i in range(1, len(pages))]]


def test_a_kit_with_no_tools_is_refused_without_calling_the_model():
    mcp, log = PagedMcp([[], [], []]), []
    out = _run(mcp, log)
    assert out == {
        "kit": "k7",
        "error": {
            "code": "NO_TOOLS",
            "message": "no tools are available for kit k7; nothing was done",
        },
    }
    assert log == []


def test_the_page_loop_is_bounded_and_fails_closed():
    class Endless(PagedMcp):
        def list_tools_sync(self, pagination_token=None):
            self.calls.append(pagination_token)
            return _Page(["k7___read_loan"], "again")

    mcp, log = Endless([]), []
    with pytest.raises(RuntimeError, match="did not end within 20 pages"):
        _run(mcp, log)
    assert len(mcp.calls) == rt.MAX_TOOL_PAGES
    assert log == []


def test_the_prompt_forbids_quoting_values_no_tool_returned():
    from awx_agent.runtime.prompt import system_prompt

    p = " ".join(system_prompt("k7").split())
    assert "that a tool returned in this conversation" in p
    assert "I could not reach the tools" in p


def test_the_prompt_says_an_auto_run_has_started():
    from awx_agent.runtime.prompt import system_prompt

    flat = " ".join(system_prompt("k1").split())
    assert (
        "SUBMITTED_NO_APPROVAL_NEEDED: the run has started without approval. The workflow "
        "rechecks policy before its first step and may still stop it, and this result does not "
        "say whether money has moved. Do not say it is waiting for approval, and do not say "
        "money has moved unless a tool says so." in flat
    )
    assert "status_message" in flat
    assert "Never say that nothing has happened" not in flat
    # The old blanket line told the model nothing had happened, whatever the status.
    assert "Say plainly that nothing has happened yet" not in flat
    assert "A proposal moves no money" not in flat


def test_the_prompt_reads_a_status_before_stating_it():
    from awx_agent.runtime.prompt import system_prompt

    flat = " ".join(system_prompt("k2").split())
    assert "Before you tell the user a proposal's current status" in flat
    assert "read it with a read_* tool in this turn" in flat
    # A propose_* retry is the only tool output carrying a status; it can start a second run.
    assert "Never call a propose_* tool to check a status" in flat
    assert (
        "If no read tool returns the status, say you have not checked it and tell the user to "
        "run `awxpoc proposals show <proposal_id>`." in flat
    )


def test_the_prompt_reports_code_sized_amounts_without_inventing_a_reason():
    from awx_agent.runtime.prompt import system_prompt

    flat = " ".join(system_prompt("k7").split())
    assert "report the amount and say the kit's code sized it" in flat
    assert "Do not give a reason for the size unless a tool returned that reason." in flat
