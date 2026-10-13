"""`awxpoc`: sign in as a demo user, chat with one kit, list proposals, approve or reject.

Configuration comes from the AgentStack outputs (`cdk deploy --outputs-file outputs.json`),
passed with --config or AWXPOC_CONFIG. Tokens are cached per user under AWXPOC_HOME
(default ~/.awxpoc) with mode 0600 and are never printed.

Approval API routes this CLI calls (served by the spine's approval API):
  GET  /proposals[?status=...]          list proposals visible to the caller
  GET  /proposals/{proposal_id}         one proposal with its canonical intent
  POST /proposals/{proposal_id}/decision {"decision": "APPROVE"|"REJECT", "intent_hash": "..."}
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from awx_common.kits import KITS

_OUTPUT_KEYS = {
    "region": "AwxRegion",
    "client_id": "AwxUserPoolClientId",
    "runtime_arn": "AwxRuntimeArn",
    "approval_api_url": "AwxApprovalApiUrl",
}
_SESSION_ID_MIN = 33  # AgentCore Runtime session IDs must be at least 33 characters


class CliError(Exception):
    """A user-facing error; printed without a traceback."""


@dataclass(frozen=True)
class Config:
    region: str
    client_id: str
    runtime_arn: str
    approval_api_url: str

    @classmethod
    def load(cls, path: str | None, env: Mapping[str, str]) -> Config:
        values: dict[str, str] = {}
        source = path or env.get("AWXPOC_CONFIG")
        if source:
            data = json.loads(Path(source).read_text())
            # `cdk deploy --outputs-file` nests outputs under the stack name.
            if not any(k in data for k in _OUTPUT_KEYS.values()):
                for stack_outputs in data.values():
                    if isinstance(stack_outputs, Mapping) and _OUTPUT_KEYS["runtime_arn"] in (
                        stack_outputs
                    ):
                        data = stack_outputs
                        break
            for field_name, key in _OUTPUT_KEYS.items():
                if key in data:
                    values[field_name] = str(data[key])
        for field_name in _OUTPUT_KEYS:
            env_key = f"AWXPOC_{field_name.upper()}"
            if env.get(env_key):
                values[field_name] = env[env_key]
        missing = sorted(set(_OUTPUT_KEYS) - set(values))
        if missing:
            raise CliError(f"missing configuration: {', '.join(missing)} (use --config)")
        return cls(**values)

    @property
    def runtime_url(self) -> str:
        arn = urllib.parse.quote(self.runtime_arn, safe="")
        return (
            f"https://bedrock-agentcore.{self.region}.amazonaws.com/runtimes/{arn}"
            "/invocations?qualifier=DEFAULT"
        )


# --- token cache ---------------------------------------------------------------------------------
def _home(env: Mapping[str, str]) -> Path:
    return Path(env.get("AWXPOC_HOME") or Path.home() / ".awxpoc")


def _token_path(env: Mapping[str, str], username: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in username)
    return _home(env) / f"tokens-{safe}.json"


def save_tokens(env: Mapping[str, str], username: str, tokens: Mapping[str, Any]) -> Path:
    home = _home(env)
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = _token_path(env, username)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(dict(tokens), fh)
    (home / "current-user").write_text(username)
    return path


def current_user(env: Mapping[str, str]) -> str:
    marker = _home(env) / "current-user"
    if not marker.exists():
        raise CliError("not signed in; run `awxpoc login --username <user>`")
    return marker.read_text().strip()


# --- Cognito -----------------------------------------------------------------------------------
def _cognito(region: str) -> Any:
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config as BotoConfig

    return boto3.client(
        "cognito-idp", region_name=region, config=BotoConfig(signature_version=UNSIGNED)
    )


def _tokens_from(result: Mapping[str, Any], refresh: str | None = None) -> dict[str, Any]:
    auth = result.get("AuthenticationResult")
    if not auth:
        challenge = result.get("ChallengeName", "unknown")
        raise CliError(f"sign-in needs challenge {challenge}; finish it in Cognito first")
    return {
        "access_token": auth["AccessToken"],
        "refresh_token": auth.get("RefreshToken") or refresh,
        "expires_at": int(time.time()) + int(auth.get("ExpiresIn", 3600)),
    }


def login(cfg: Config, username: str, password: str, cognito: Any) -> dict[str, Any]:
    try:
        result = cognito.initiate_auth(
            ClientId=cfg.client_id,
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": username, "PASSWORD": password},
        )
    except Exception as exc:  # botocore ClientError; never echo the request
        raise CliError(f"sign-in failed: {type(exc).__name__}") from None
    return _tokens_from(result)


def access_token(cfg: Config, env: Mapping[str, str], cognito_factory: Callable[[str], Any]) -> str:
    username = current_user(env)
    path = _token_path(env, username)
    if not path.exists():
        raise CliError("not signed in; run `awxpoc login`")
    tokens = json.loads(path.read_text())
    if tokens.get("expires_at", 0) - 60 > time.time():
        return str(tokens["access_token"])
    if not tokens.get("refresh_token"):
        raise CliError("session expired; run `awxpoc login` again")
    try:
        result = cognito_factory(cfg.region).initiate_auth(
            ClientId=cfg.client_id,
            AuthFlow="REFRESH_TOKEN_AUTH",
            AuthParameters={"REFRESH_TOKEN": tokens["refresh_token"]},
        )
    except Exception:
        raise CliError("session expired; run `awxpoc login` again") from None
    fresh = _tokens_from(result, refresh=tokens["refresh_token"])
    save_tokens(env, username, fresh)
    return str(fresh["access_token"])


# --- HTTP --------------------------------------------------------------------------------------
Http = Callable[[str, str, Mapping[str, str], bytes | None], tuple[int, bytes]]


def _http(
    method: str, url: str, headers: Mapping[str, str], body: bytes | None
) -> tuple[int, bytes]:
    if not url.startswith("https://"):
        raise CliError("refusing a non-HTTPS endpoint")
    req = urllib.request.Request(url, data=body, method=method, headers=dict(headers))  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310 - https only, above
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _json_call(http: Http, method: str, url: str, token: str, body: Any = None) -> Any:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    status, raw = http(method, url, headers, data)
    try:
        parsed = json.loads(raw or b"null")
    except ValueError:
        parsed = {"raw": raw.decode("utf-8", "replace")[:500]}
    if status >= 400:
        raise CliError(f"HTTP {status}: {json.dumps(parsed)[:500]}")
    return parsed


def new_session_id() -> str:
    sid = f"awxpoc-{uuid.uuid4()}"
    assert len(sid) >= _SESSION_ID_MIN
    return sid


# --- commands ----------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="awxpoc", description=__doc__.split("\n\n")[0])
    p.add_argument("--config", help="AgentStack outputs JSON (cdk deploy --outputs-file)")
    sub = p.add_subparsers(dest="command", required=True)

    lg = sub.add_parser("login", help="sign in as a demo user (requester or approver)")
    lg.add_argument("--username", required=True)
    lg.add_argument("--password-stdin", action="store_true", help="read the password from stdin")

    ch = sub.add_parser("chat", help="talk to the agent for one kit")
    ch.add_argument("--kit", required=True, choices=sorted(KITS))
    ch.add_argument("--message", "-m", help="send one message and exit; omit for a prompt loop")
    ch.add_argument("--session", help="Runtime session ID to continue (33+ characters)")

    pr = sub.add_parser("proposals", help="list or show proposals")
    prs = pr.add_subparsers(dest="proposals_command", required=True)
    ls = prs.add_parser("list")
    ls.add_argument("--status", choices=["PENDING", "APPROVED", "REJECTED", "EXPIRED", "BLOCKED"])
    sh = prs.add_parser("show")
    sh.add_argument("proposal_id")

    for name in ("approve", "reject"):
        d = sub.add_parser(name, help=f"{name} a proposal (must not be your own)")
        d.add_argument("proposal_id")
        d.add_argument(
            "--intent-hash", required=True, help="the hash you reviewed; must match the proposal"
        )
    return p


def _validate(args: argparse.Namespace) -> None:
    if getattr(args, "session", None) and len(args.session) < _SESSION_ID_MIN:
        raise CliError(f"--session must be at least {_SESSION_ID_MIN} characters")
    h = getattr(args, "intent_hash", None)
    if h is not None and (len(h) != 64 or any(c not in "0123456789abcdef" for c in h)):
        raise CliError("--intent-hash must be a 64-character lowercase SHA-256 hex digest")
    pid = getattr(args, "proposal_id", None)
    if pid is not None:
        try:
            uuid.UUID(pid)
        except ValueError:
            raise CliError("proposal_id must be a UUID") from None


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    http: Http = _http,
    cognito_factory: Callable[[str], Any] = _cognito,
    stdin: Any = None,
    stdout: Any = None,
    stderr: Any = None,
) -> int:
    env = os.environ if env is None else env
    stdin = stdin or sys.stdin
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    args = build_parser().parse_args(argv)
    try:
        _validate(args)
        cfg = Config.load(args.config, env)
        if args.command == "login":
            password = stdin.readline().rstrip("\n") if args.password_stdin else getpass.getpass()
            tokens = login(cfg, args.username, password, cognito_factory(cfg.region))
            save_tokens(env, args.username, tokens)
            print(f"signed in as {args.username}", file=out)
            return 0
        token = access_token(cfg, env, cognito_factory)
        base = cfg.approval_api_url.rstrip("/")
        if args.command == "chat":
            session = args.session or new_session_id()
            print(f"session {session}", file=out)
            messages = [args.message] if args.message else _prompt_loop(stdin, out)
            for message in messages:
                status, raw = http(
                    "POST",
                    cfg.runtime_url,
                    {
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session,
                    },
                    json.dumps({"kit": args.kit, "prompt": message}).encode(),
                )
                if status >= 400:
                    raise CliError(f"agent returned HTTP {status}")
                reply = json.loads(raw)
                if "error" in reply:
                    raise CliError(f"{reply['error'].get('code')}: {reply['error'].get('message')}")
                print(reply.get("reply", ""), file=out)
                # The model that answered goes to stderr, keeping it out of the reply on stdout.
                print(f"(answered by {reply.get('model') or 'an unknown model'})", file=err)
            return 0
        if args.command == "proposals":
            if args.proposals_command == "list":
                query = f"?status={args.status}" if args.status else ""
                result = _json_call(http, "GET", f"{base}/proposals{query}", token)
            else:
                result = _json_call(http, "GET", f"{base}/proposals/{args.proposal_id}", token)
            print(json.dumps(result, indent=2, sort_keys=True), file=out)
            return 0
        if args.command in ("approve", "reject"):
            result = _json_call(
                http,
                "POST",
                f"{base}/proposals/{args.proposal_id}/decision",
                token,
                {"decision": args.command.upper(), "intent_hash": args.intent_hash},
            )
            print(json.dumps(result, indent=2, sort_keys=True), file=out)
            return 0
    except CliError as exc:
        print(f"awxpoc: {exc}", file=sys.stderr)
        return 2
    return 1  # pragma: no cover - argparse requires a command


def _prompt_loop(stdin: Any, out: Any):
    while True:
        print("you> ", end="", file=out, flush=True)
        line = stdin.readline()
        if not line or line.strip() in {"/quit", "/exit"}:
            return
        if line.strip():
            yield line.strip()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
