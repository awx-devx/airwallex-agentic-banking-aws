"""AgentCore Runtime entrypoint: a Strands agent with one kit's Gateway tools.

Inbound: the Runtime's Cognito JWT authorizer verifies the caller's access token and, because
`Authorization` is on the Runtime's request-header allowlist, passes it to this code.
Outbound to the Gateway: the agent presents the same user token. The Gateway verifies it again
with its own Cognito authorizer and its interceptor turns it into the caller identity the tool
Lambdas use. The Runtime's own IAM role therefore needs no permission on any tool, reader,
adapter or secret; it can only call the Bedrock model.

Payload: {"kit": "k1", "prompt": "..."}. The session ID header keeps the conversation.
"""

from __future__ import annotations

import logging
import os
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping
from typing import Any

from awx_common.kits import KITS
from awx_common.regions import DEFAULT_REGION, UnsupportedRegionError, default_model_ids
from bedrock_agentcore.runtime import BedrockAgentCoreApp

from ..identity import jwt_claims_unverified
from ..registry import TOOL_TARGET_DELIMITER
from .prompt import system_prompt

log = logging.getLogger(__name__)

# Local fallbacks only: the deployed Runtime always gets AWX_REGION and the model IDs.
DEFAULT_MODEL_ID, DEFAULT_FALLBACK_MODEL_ID = default_model_ids(DEFAULT_REGION)
_FALLBACK_CODES = frozenset(
    {"AccessDeniedException", "ValidationException", "ResourceNotFoundException"}
)
_MAX_SESSIONS = 64
_MAX_PROMPT_CHARS = 16_000

app = BedrockAgentCoreApp()


def _local_model_ids(region: str) -> tuple[str, str]:
    """The Region's default inference profiles, or the default Region's for a Region with none."""
    try:
        return default_model_ids(region)
    except UnsupportedRegionError:
        return DEFAULT_MODEL_ID, DEFAULT_FALLBACK_MODEL_ID


class InvocationError(ValueError):
    """Bad request to the agent. Safe to return to the caller."""


class _Sessions:
    """Conversation history per Runtime session, bounded. A session lives in one microVM."""

    def __init__(self, limit: int = _MAX_SESSIONS):
        self._limit = limit
        self._data: OrderedDict[tuple[str, str, str], list[Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: tuple[str, str, str]) -> list[Any]:
        with self._lock:
            messages = self._data.pop(key, [])
            self._data[key] = messages
            while len(self._data) > self._limit:
                self._data.popitem(last=False)
            return messages

    def put(self, key: tuple[str, str, str], messages: list[Any]) -> None:
        with self._lock:
            self._data[key] = messages


SESSIONS = _Sessions()


def parse_payload(payload: Any) -> tuple[str, str]:
    if not isinstance(payload, Mapping):
        raise InvocationError("payload must be a JSON object")
    kit = payload.get("kit")
    prompt = payload.get("prompt")
    if kit not in KITS:
        raise InvocationError(f"kit must be one of {sorted(KITS)}")
    if not isinstance(prompt, str) or not prompt.strip():
        raise InvocationError("prompt is required")
    if len(prompt) > _MAX_PROMPT_CHARS:
        raise InvocationError("prompt is too long")
    return str(kit), prompt


def bearer_token(headers: Mapping[str, str] | None) -> str:
    for key, value in (headers or {}).items():
        if key.lower() == "authorization" and value.lower().startswith("bearer "):
            return value
    raise InvocationError("no bearer token reached the agent; call the Runtime with a JWT")


def kit_tool_filter(kit: str) -> dict[str, list[Any]]:
    """Only this kit's target, and only read_ and propose_ tools within it."""
    import re

    prefix = re.escape(f"{kit}{TOOL_TARGET_DELIMITER}")
    return {"allowed": [re.compile(rf"^{prefix}(read|propose)_[a-z0-9_]+$")]}


def _default_mcp_client(gateway_url: str, token: str, kit: str) -> Any:
    from strands.tools.mcp import MCPClient

    return MCPClient(
        url=gateway_url,
        headers={"Authorization": token},
        tool_filters=kit_tool_filter(kit),
        application_name="awx-agent",
    )


def _default_agent(model_id: str, region: str, kit: str, tools: list[Any], messages: list[Any]):
    from strands import Agent
    from strands.models import BedrockModel

    return Agent(
        model=BedrockModel(model_id=model_id, region_name=region, temperature=0),
        system_prompt=system_prompt(kit),
        tools=tools,
        messages=messages,
        callback_handler=None,
    )


McpFactory = Callable[[str, str, str], Any]
AgentFactory = Callable[[str, str, str, list[Any], list[Any]], Any]


def _used_tools(messages: list[Any]) -> bool:
    for message in messages:
        for block in (message or {}).get("content", []) or []:
            if isinstance(block, Mapping) and "toolUse" in block:
                return True
    return False


def _error_code(exc: BaseException) -> str | None:
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        response = getattr(cur, "response", None)
        if isinstance(response, Mapping):
            code = (response.get("Error") or {}).get("Code")
            if code:
                return str(code)
        cur = cur.__cause__ or cur.__context__
    return None


MAX_TOOL_PAGES = 20  # the Gateway pages tools/list (30 per page today; 61 tools in 3 pages)


def list_all_tools(client: Any, *, max_pages: int = MAX_TOOL_PAGES) -> list[Any]:
    """Every page of the client's (kit-filtered) tool list.

    The Gateway pages tools/list, and a single list_tools_sync() returns only the first page
    (kits 1-4), which would leave a kit 5-8 chat with no tools and a model that invents
    proposals. Follow pagination_token to the end; fail closed past max_pages."""
    tools: list[Any] = []
    token: str | None = None
    for n in range(max_pages):
        page = (
            client.list_tools_sync() if n == 0 else client.list_tools_sync(pagination_token=token)
        )
        tools.extend(page)
        token = getattr(page, "pagination_token", None)
        if not token:
            return tools
    raise RuntimeError(f"the Gateway tool list did not end within {max_pages} pages")


def run_turn(
    *,
    kit: str,
    prompt: str,
    token: str,
    session_id: str,
    env: Mapping[str, str],
    mcp_factory: McpFactory = _default_mcp_client,
    agent_factory: AgentFactory = _default_agent,
) -> dict[str, Any]:
    gateway_url = env["AWX_GATEWAY_URL"]
    region = env.get("AWX_REGION", DEFAULT_REGION)
    default_primary, default_fallback = _local_model_ids(region)
    models = [env.get("AWX_MODEL_ID", default_primary)]
    fallback = env.get("AWX_FALLBACK_MODEL_ID", default_fallback)
    if fallback and fallback not in models:
        models.append(fallback)
    # The caller's sub is part of the key so one user's history never serves another user.
    # The Runtime's inbound authorizer has verified this token already.
    key = (session_id, kit, str(jwt_claims_unverified(token).get("sub", "")))
    history = SESSIONS.get(key)
    client = mcp_factory(gateway_url, token, kit)
    with client:
        tools = list_all_tools(client)
        if not tools:  # never let the model answer a kit with no tools (it would invent results)
            log.warning("no tools listed for kit %s; turn refused", kit)
            return {
                "kit": kit,
                "error": {
                    "code": "NO_TOOLS",
                    "message": f"no tools are available for kit {kit}; nothing was done",
                },
            }
        last_exc: BaseException | None = None
        for model_id in models:
            messages = list(history)
            agent = agent_factory(model_id, region, kit, tools, messages)
            try:
                result = agent(prompt)
            except Exception as exc:
                last_exc = exc
                code = _error_code(exc)
                if code in _FALLBACK_CODES and not _used_tools(agent.messages[len(history) :]):
                    log.warning("model %s unavailable (%s); trying fallback", model_id, code)
                    continue
                raise
            SESSIONS.put(key, list(agent.messages))
            return {"kit": kit, "model": model_id, "reply": str(result)}
    raise RuntimeError("no model could answer") from last_exc


@app.entrypoint
def invoke(payload: Any, context: Any) -> dict[str, Any]:
    try:
        kit, prompt = parse_payload(payload)
        token = bearer_token(getattr(context, "request_headers", None))
    except InvocationError as exc:
        return {"error": {"code": "BAD_REQUEST", "message": str(exc)}}
    session_id = getattr(context, "session_id", None) or "default"
    return run_turn(kit=kit, prompt=prompt, token=token, session_id=session_id, env=os.environ)


if __name__ == "__main__":
    app.run()
