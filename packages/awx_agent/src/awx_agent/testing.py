"""Run a kit's read_* and propose_* tools through the real ToolDispatcher with no AWS.

    from awx_agent.testing import KitToolHarness
    from awx_spine.fake import FakeAirwallex

    h = KitToolHarness.for_kit("k1", port=FakeAirwallex(balances=...))
    seed_book(h.store(), DEMO_BOOK)              # kit state for the platform tenant
    out = h.call("read_balances", {})            # a requester, kits 1-4 run as "platform"
    out = h.call("propose_execute_plan", {...})  # -> proposal_id, risk_tier, status ...
    h.executions                                 # decoded StartExecution inputs

Everything the dispatcher touches is the production code path except four stand-ins:

- FakeReaderLambda answers the per-kit reader's `invoke` the way handlers/reader.py does
  (server-side tenant resolution, scope derived from x-on-behalf-of, the kit's allowlists)
  and reads through any AirwallexPort, normally awx_spine.fake.FakeAirwallex;
- RecordingSfn records StartExecution with Step Functions' name semantics (same name and
  input: idempotent; same name, other input: ExecutionAlreadyExists) and answers
  DescribeExecution;
- TokenIssuer signs Cognito-shaped RS256 access tokens and serves the matching JWKS, so the
  dispatcher's real CognitoVerifier checks a real signature;
- InMemoryKitStore per (kit, tenant) and a dict TenantResolver.

No network, no boto3 client, no AWS credentials.
"""

from __future__ import annotations

import base64
import io
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from awx_common import transport
from awx_common.errors import TenantScopeViolation
from awx_common.ops import AirwallexPort
from awx_common.store import InMemoryKitStore, KitStore
from awx_common.tenancy import StaticTenantResolver, bind_read, resolve_in_kit
from awx_spine_aws import codec, engine

from .handlers.tools import GatewayToolEnv, ToolDispatcher
from .identity import APPROVER_GROUP, IDENTITY_FIELD, PLATFORM_TENANT, REQUESTER_GROUP
from .jwtverify import CognitoVerifier, JwksCache
from .registry import TOOL_TARGET_DELIMITER, KitTools, load_kit_tools

ISSUER = "https://cognito-idp.ap-southeast-2.amazonaws.com/ap-southeast-2_TESTPOOL"
CLIENT_ID = "test-agent-client"
KID = "test-kid"
READER_FN = "fake-reader"
ADAPTER_FN = "fake-adapter"  # never invoked; tests assert it stays that way
ACCOUNT = "111111111111"


def _seg(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _int(n: int) -> str:
    return _seg(n.to_bytes((n.bit_length() + 7) // 8, "big"))


class TokenIssuer:
    """Signs RS256 access tokens for a test user pool; `verifier()` trusts exactly this key."""

    def __init__(
        self,
        *,
        issuer: str = ISSUER,
        client_id: str = CLIENT_ID,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """`clock` (a timestamp source) is what `token()` signs with when no `now=` is given;
        KitToolHarness passes its own clock so issuer and verifier always agree."""
        from cryptography.hazmat.primitives.asymmetric import rsa

        self.issuer = issuer
        self.client_id = client_id
        self.clock = clock
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def jwks(self) -> dict[str, Any]:
        pub = self._key.public_key().public_numbers()
        return {
            "keys": [
                {
                    "kty": "RSA",
                    "kid": KID,
                    "alg": "RS256",
                    "use": "sig",
                    "n": _int(pub.n),
                    "e": _int(pub.e),
                }
            ]
        }

    def token(
        self,
        sub: str = "user-requester",
        *,
        groups: tuple[str, ...] = (REQUESTER_GROUP,),
        tenant_id: str | None = None,
        now: float | None = None,
        **claims: Any,
    ) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        t = int(self.clock() if now is None else now)
        body = {
            "iss": self.issuer,
            "token_use": "access",
            "client_id": self.client_id,
            "sub": sub,
            "username": sub,
            "cognito:groups": list(groups),
            "iat": t - 5,
            "exp": t + 3600,
            **({"tenant_id": tenant_id} if tenant_id else {}),
            **claims,
        }
        head = _seg(json.dumps({"alg": "RS256", "kid": KID}).encode())
        payload = _seg(json.dumps(body).encode())
        signing = f"{head}.{payload}".encode()
        sig = self._key.sign(signing, padding.PKCS1v15(), hashes.SHA256())
        return f"{head}.{payload}.{_seg(sig)}"

    def verifier(self, clock: Callable[[], float] = time.time) -> CognitoVerifier:
        cache = JwksCache(
            f"{self.issuer}/.well-known/jwks.json", fetch=lambda _u: self.jwks(), clock=clock
        )
        return CognitoVerifier(
            issuer=self.issuer, client_id=self.client_id, jwks=cache, clock=clock
        )


class DictTenantResolver(StaticTenantResolver):
    """tenant_id -> connected account (None: an operator) or a kit-bound TenantRow. A plain entry
    binds to the harness's kit; a TenantRow to its own. "platform" is known for kits 1 to 4."""


class FakeReaderLambda:
    """The per-kit reader (SpineStack.reader_for(kit)) as a lambda client: same scope rules as
    awx_spine_aws.handlers.reader, reading through `port`. Records every call."""

    def __init__(
        self, *, kit: str, plan: KitTools, port: AirwallexPort, resolver: DictTenantResolver
    ) -> None:
        self.kit = kit
        self.port = port
        self.resolver = resolver
        self.tenant_patterns: list[str] = []
        self.platform_patterns: list[str] = []
        for spec in plan.specs.values():
            self.tenant_patterns += [
                p for p in engine.allowlist(spec) if p not in self.tenant_patterns
            ]
            self.platform_patterns += [
                p for p in engine.platform_allowlist(spec) if p not in self.platform_patterns
            ]
        self.calls: list[dict[str, Any]] = []

    def invoke(self, *, FunctionName: str, InvocationType: str, Payload: bytes) -> dict[str, Any]:
        payload = transport.loads(Payload.decode())
        self.calls.append({"FunctionName": FunctionName, "payload": payload})
        try:
            if FunctionName != READER_FN:
                raise TenantScopeViolation(f"only the {self.kit} reader may be invoked")
            if payload.get("kit") not in (None, self.kit):
                raise TenantScopeViolation(f"this reader serves {self.kit}")
            req = codec.read_request_from(payload["read"])
            tenant = resolve_in_kit(self.resolver, payload.get("tenant_id"), self.kit)
            if tenant.connected_account_id is None:
                if req.on_behalf_of is not None:
                    raise TenantScopeViolation("the platform tenant may not send x-on-behalf-of")
                patterns = engine.own_account_patterns(self.tenant_patterns, self.platform_patterns)
            elif req.on_behalf_of is None:
                patterns = self.platform_patterns
            else:
                req = bind_read(req, tenant)
                patterns = self.tenant_patterns
            if not engine.path_allowed(patterns, req.path):
                raise TenantScopeViolation(f"{self.kit} may not read {req.path}")
            body = self.port.read(req)
        except Exception as exc:  # the Lambda runtime turns a raise into FunctionError
            err = json.dumps({"errorType": type(exc).__name__, "errorMessage": str(exc)[:300]})
            return {"FunctionError": "Unhandled", "Payload": io.BytesIO(err.encode())}
        return {"Payload": io.BytesIO(transport.dumps({"body": body}).encode())}


class _AlreadyExists(Exception):
    def __init__(self) -> None:
        super().__init__("ExecutionAlreadyExists")
        self.response = {"Error": {"Code": "ExecutionAlreadyExists"}}


class RecordingSfn:
    """StartExecution / DescribeExecution with Standard-workflow name semantics (same name and
    input as a RUNNING execution: idempotent; closed or different input: ExecutionAlreadyExists).
    Set `status[arn]` to SUCCEEDED, FAILED, ABORTED ... to close an execution. It has no
    SendTask* method, so nothing a tool does can release an approval wait."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.executions: dict[str, str] = {}  # execution ARN -> input JSON
        self.status: dict[str, str] = {}  # execution ARN -> status (default RUNNING)

    def start_execution(self, *, stateMachineArn: str, name: str, input: str) -> dict[str, Any]:
        self.calls.append({"stateMachineArn": stateMachineArn, "name": name, "input": input})
        arn = stateMachineArn.replace(":stateMachine:", ":execution:") + f":{name}"
        # StartExecution: the same name and input as a RUNNING execution succeeds; a closed
        # execution, or a different input, is ExecutionAlreadyExists (API reference).
        if arn in self.executions and (
            self.executions[arn] != input or self.status.get(arn, "RUNNING") != "RUNNING"
        ):
            raise _AlreadyExists()
        self.executions.setdefault(arn, input)
        return {"executionArn": arn, "startDate": datetime.now(UTC)}

    def describe_execution(self, *, executionArn: str) -> dict[str, Any]:
        return {
            "executionArn": executionArn,
            "status": self.status.get(executionArn, "RUNNING"),
            "input": self.executions[executionArn],
        }


@dataclass
class KitToolHarness:
    kit: str
    dispatcher: ToolDispatcher
    reader: FakeReaderLambda
    sfn: RecordingSfn
    issuer: TokenIssuer
    stores: dict[tuple[str, str], KitStore] = field(default_factory=dict)
    outcomes: dict[str, str] = field(default_factory=dict)  # proposal_id -> recorded outcome
    # The one clock the harness signs and verifies tokens with: a kit test that fixes
    # the clock gets tokens issued at that time, however long after collection it runs.
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    @classmethod
    def for_kit(
        cls,
        kit: str,
        *,
        port: AirwallexPort,
        tools_module: str | None = None,
        tenants: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> KitToolHarness:
        """`tools_module` defaults to the kit's own `awx_k<N>_<slug>.tools`."""
        from awx_common.kits import KIT_DIRS

        module = tools_module or f"awx_{KIT_DIRS[kit].rsplit('/', 1)[1]}.tools"
        plan = load_kit_tools(kit, module)
        resolver = DictTenantResolver(tenants, kit=kit)
        reader = FakeReaderLambda(kit=kit, plan=plan, port=port, resolver=resolver)
        sfn = RecordingSfn()
        now = clock or (lambda: datetime.now(UTC))
        issuer = TokenIssuer(clock=lambda: now().timestamp())  # signs at the harness clock too
        stores: dict[tuple[str, str], KitStore] = {}
        outcomes: dict[str, str] = {}
        env = GatewayToolEnv(
            kit=kit,
            lambda_client=reader,
            reader_fn=READER_FN,
            resolver=resolver,
            store_factory=lambda k, t: stores.setdefault((k, t.tenant_id), InMemoryKitStore()),
            clock=now,
        )
        dispatcher = ToolDispatcher(
            kit=kit,
            tools={t.name: t for t in plan.tools},
            specs=plan.specs,
            reads=plan.reads,
            env=env,
            sfn_client=sfn,
            state_machines={
                a: f"arn:aws:states:ap-southeast-2:{ACCOUNT}:stateMachine:{kit}-{a}"
                for a in plan.propose_actions
            },
            verifier=issuer.verifier(clock=lambda: now().timestamp()),
            proposal_outcome=outcomes.get,
        )
        return cls(kit, dispatcher, reader, sfn, issuer, stores, outcomes, clock=now)

    def store(self, tenant_id: str = PLATFORM_TENANT) -> KitStore:
        """The kit's store for one tenant (seed kit state here before calling tools)."""
        return self.stores.setdefault((self.kit, tenant_id), InMemoryKitStore())

    def call(
        self,
        tool: str,
        arguments: Mapping[str, Any],
        *,
        user: str = "user-requester",
        groups: tuple[str, ...] = (REQUESTER_GROUP,),
        tenant_id: str | None = None,
        token: str | None = None,
    ) -> dict[str, Any]:
        """Call one tool as the Gateway would: `<kit>___<tool>` with the verified token in
        the identity field. Returns the tool's JSON result (or {"error": {...}})."""
        event = dict(arguments)
        event[IDENTITY_FIELD] = token or self.token(user, groups=groups, tenant_id=tenant_id)
        return self.dispatcher.dispatch(f"{self.kit}{TOOL_TARGET_DELIMITER}{tool}", event)

    def token(
        self,
        sub: str = "user-requester",
        *,
        groups: tuple[str, ...] = (REQUESTER_GROUP,),
        tenant_id: str | None = None,
        **claims: Any,
    ) -> str:
        """A token issued at the harness clock's time, so it verifies against that clock.
        Pass `now=` (a timestamp) to issue at another time, e.g. an expired token."""
        claims.setdefault("now", self.clock().timestamp())
        return self.issuer.token(sub, groups=groups, tenant_id=tenant_id, **claims)

    @property
    def executions(self) -> list[dict[str, Any]]:
        """Decoded StartExecution inputs, one per execution started."""
        return [transport.loads(raw) for raw in self.sfn.executions.values()]


__all__ = [
    "ADAPTER_FN",
    "APPROVER_GROUP",
    "REQUESTER_GROUP",
    "DictTenantResolver",
    "FakeReaderLambda",
    "KitToolHarness",
    "RecordingSfn",
    "TokenIssuer",
]
