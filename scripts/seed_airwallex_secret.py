"""Put the Airwallex sandbox credentials into a deployment's Secrets Manager secret.

Run from the repo root with operator AWS credentials for the deploy account:
    uv run --all-packages python scripts/seed_airwallex_secret.py --profile awx-deploy \\
        --region <Region>
    ... scripts/seed_airwallex_secret.py --profile awx-deploy --region <Region> --from-env

It prompts for the client ID and the API key without echoing them, or with --from-env reads
AWX_CLIENT_ID and AWX_API_KEY from its environment, and calls Secrets Manager PutSecretValue
with a JSON object holding `client_id` and `api_key`. No value is written to a file, put on a
command line or printed.

The Region is --region, else AwxAgentStack.AwxRegion in infra/cdk-outputs.json, else the
script refuses. The secret is --secret-id, else AwxSpine.AirwallexSecretArn in the outputs
file when that Amazon Resource Name (ARN) is in the Region, else the AirwallexSecretArn output
of the AwxSpine stack in the Region. An ARN in another Region is never seeded.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

# The file `cdk deploy --outputs-file cdk-outputs.json` writes when run from infra/.
DEFAULT_OUTPUTS = Path(__file__).resolve().parents[1] / "infra" / "cdk-outputs.json"
SPINE_STACK = "AwxSpine"
ARN_OUTPUT = "AirwallexSecretArn"
CLIENT_ID_VAR = "AWX_CLIENT_ID"
API_KEY_VAR = "AWX_API_KEY"


class SeedError(Exception):
    """A refusal, reported as one line with no credential value in it."""


EXPIRED = "your AWS sign-in has expired; sign in again for that profile and retry"
INVALID = "the credentials are not valid; check --profile"
UNREACHABLE = "cannot reach AWS in {region}; check --region"
DENIED = (
    "these credentials might not call {operation}; use operator credentials for the deploy account"
)
# A fixed next step for each common AWS failure, by exception type or AWS error code. Only the
# operation name and the Region are filled in.
ERROR_HINTS = {
    "NoCredentialsError": (
        "no AWS credentials found; pass --profile with your operator profile, or sign in for it "
        "and retry"
    ),
    "ProfileNotFound": "the --profile is not in your AWS config; pass a profile that exists",
    "ExpiredToken": EXPIRED,
    "ExpiredTokenException": EXPIRED,
    "TokenRetrievalError": EXPIRED,
    "UnauthorizedSSOTokenError": EXPIRED,
    "SSOTokenLoadError": EXPIRED,
    "AccessDenied": DENIED,
    "AccessDeniedException": DENIED,
    "UnrecognizedClientException": INVALID,
    "InvalidClientTokenId": INVALID,
    "ResourceNotFoundException": (
        "no such secret in {region}; check --region, or pass --secret-id <ARN> from the "
        f"{SPINE_STACK} stack's outputs"
    ),
    "EndpointConnectionError": UNREACHABLE,
    "InvalidRegionError": UNREACHABLE,
}


def error_line(exc: Exception, region: str) -> str:
    """The exception type, any AWS error code and operation, and a fixed hint. Never the message,
    which can echo the request."""
    line = type(exc).__name__
    hint = ERROR_HINTS.get(line)
    operation = "this operation"
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", "no error code"))
        operation = str(exc.operation_name)
        line += f" ({code}) from {operation}"
        hint = ERROR_HINTS.get(code, hint)
    if hint:
        line += "; " + hint.format(operation=operation, region=region or "the Region")
    return line


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--profile", help="the AWS CLI profile to use (the docs use awx-deploy)")
    parser.add_argument(
        "--region",
        help="the deployment's Region (default: AwxAgentStack.AwxRegion in the outputs file)",
    )
    parser.add_argument(
        "--outputs",
        type=Path,
        default=DEFAULT_OUTPUTS,
        help="stack outputs JSON written by cdk deploy --outputs-file "
        "(default: infra/cdk-outputs.json)",
    )
    parser.add_argument(
        "--secret-id",
        help="the secret's Amazon Resource Name (ARN) or name (default: "
        "AwxSpine.AirwallexSecretArn in the outputs file, else the AwxSpine stack's output)",
    )
    parser.add_argument(
        "--from-env",
        action="store_true",
        help=f"read {CLIENT_ID_VAR} and {API_KEY_VAR} from the environment, for a shell with "
        "no terminal",
    )
    return parser.parse_args(argv)


def read_outputs(path: Path) -> dict[str, Any]:
    """The outputs file's contents, or {} when it does not exist."""
    if not path.is_file():
        return {}
    try:
        outputs = json.loads(path.read_text())
    except ValueError as exc:
        raise SeedError(
            f"{path}: not valid JSON ({exc.msg}); fix or delete it, or pass --outputs with "
            "another file"
        ) from None
    if not isinstance(outputs, dict):
        raise SeedError(f"{path}: not a JSON object")
    return outputs


def _output(outputs: Mapping[str, Any], stack: str, key: str) -> str:
    value = outputs.get(stack, {})
    value = value.get(key) if isinstance(value, dict) else None
    return value.strip() if isinstance(value, str) else ""


def resolve_region(region: str | None, outputs: Mapping[str, Any], path: Path) -> str:
    """--region, else AwxAgentStack.AwxRegion in the outputs file, else a refusal."""
    if region and region.strip():
        return region.strip()
    recorded = _output(outputs, "AwxAgentStack", "AwxRegion")
    if recorded:
        return recorded
    where = f"{path} holds no AwxAgentStack.AwxRegion" if path.is_file() else f"no {path}"
    raise SeedError(
        f"no Region: pass --region with the Region the stacks were deployed to ({where})"
    )


def _arn_region(arn: str) -> str:
    parts = arn.split(":")
    return parts[3] if len(parts) > 3 and parts[0] == "arn" else ""


def _check_arn_region(arn: str, region: str, source: str) -> None:
    """Refuse an ARN in another Region, or an ARN with no Region in it."""
    recorded = _arn_region(arn)
    if not recorded:
        raise SeedError(
            f"{source} holds a secret ARN with no Region in it; pass --secret-id <ARN> for the "
            f"secret in {region}"
        )
    if recorded != region:
        raise SeedError(
            f"{source} names the secret of a deployment in {recorded}, not {region}; pass "
            f"--region {recorded} to seed that deployment, or --secret-id <ARN> to seed the "
            f"{region} one"
        )


def resolve_secret_id(
    secret_id: str | None,
    outputs: Mapping[str, Any],
    region: str,
    session: Any,
    path: Path,
) -> str:
    """--secret-id, else the outputs file's ARN in `region`, else the spine stack's output."""
    if secret_id and secret_id.strip():
        secret_id = secret_id.strip()
        if secret_id.startswith("arn:"):
            _check_arn_region(secret_id, region, "--secret-id")
        return secret_id
    arn = _output(outputs, SPINE_STACK, ARN_OUTPUT)
    if arn:
        recorded = _arn_region(arn)
        if recorded == region:
            return arn
        if not recorded:
            _check_arn_region(arn, region, str(path))
        print(
            f"seed_airwallex_secret: {path} belongs to a deployment in {recorded}; looking up "
            f"the {SPINE_STACK} stack in {region}",
            file=sys.stderr,
        )
    # The outputs file is written once the whole deploy has finished; the spine stack's
    # outputs exist as soon as it has deployed.
    try:
        stacks = session.client("cloudformation").describe_stacks(StackName=SPINE_STACK)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ValidationError":
            raise
        raise SeedError(
            f"no {SPINE_STACK} stack in {region}; pass --region with the Region the stacks were "
            "deployed to, or seed after the spine stack has deployed"
        ) from None
    for output in stacks["Stacks"][0].get("Outputs", []) if stacks["Stacks"] else []:
        if output.get("OutputKey") == ARN_OUTPUT and output.get("OutputValue"):
            return str(output["OutputValue"])
    raise SeedError(
        f"stack {SPINE_STACK} in {region} has no {ARN_OUTPUT} output; wait for the spine stack "
        "to finish deploying, or pass --secret-id <ARN>"
    )


def _has_terminal() -> bool:
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    except OSError:
        return False
    os.close(fd)
    return True


def read_credentials(
    from_env: bool,
    environ: Mapping[str, str] | MutableMapping[str, str],
    prompt: Callable[[str], str],
    has_terminal: Callable[[], bool],
) -> dict[str, str]:
    """The client ID and API key, from the environment or from prompts. Refuses an empty one."""
    if from_env:
        values = {
            "client_id": environ.get(CLIENT_ID_VAR, ""),
            "api_key": environ.get(API_KEY_VAR, ""),
        }
        names = {"client_id": CLIENT_ID_VAR, "api_key": API_KEY_VAR}
        # Keep the values out of the environment any credential process inherits.
        if isinstance(environ, MutableMapping):
            environ.pop(CLIENT_ID_VAR, None)
            environ.pop(API_KEY_VAR, None)
    else:
        if not has_terminal():
            raise SeedError(
                f"no terminal to prompt on; set {CLIENT_ID_VAR} and {API_KEY_VAR} and pass "
                "--from-env"
            )
        values = {
            "client_id": prompt("Airwallex client ID: "),
            "api_key": prompt("Airwallex API key: "),
        }
        names = {"client_id": "the client ID", "api_key": "the API key"}
    values = {key: value.strip() for key, value in values.items()}
    empty = [names[key] for key, value in values.items() if not value]
    if empty:
        raise SeedError(f"{' and '.join(empty)} must not be empty; nothing was uploaded")
    return values


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    prompt: Callable[[str], str] = getpass.getpass,
    has_terminal: Callable[[], bool] = _has_terminal,
    session_factory: Callable[..., Any] = boto3.Session,
) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    uploading = False
    region = ""
    environ = os.environ if environ is None else environ
    try:
        outputs = read_outputs(args.outputs)
        region = resolve_region(args.region, outputs, args.outputs)
        # With --from-env the variables are read and removed before any AWS call, so a
        # credential process never inherits them. Prompts wait until the secret is resolved,
        # so a refusal comes before any prompt.
        values = read_credentials(True, environ, prompt, has_terminal) if args.from_env else None
        session = session_factory(profile_name=args.profile, region_name=region)
        secret_id = resolve_secret_id(args.secret_id, outputs, region, session, args.outputs)
        if values is None:
            values = read_credentials(False, environ, prompt, has_terminal)
        uploading = True
        result = session.client("secretsmanager").put_secret_value(
            SecretId=secret_id, SecretString=json.dumps(values)
        )
    except (KeyboardInterrupt, EOFError):
        if uploading:
            print(
                "\ncancelled during the upload; the secret might hold the new values. Run the "
                "script again to be sure",
                file=sys.stderr,
            )
        else:
            print("\ncancelled; nothing was uploaded", file=sys.stderr)
        return 130
    except SeedError as exc:
        print(f"seed_airwallex_secret: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"seed_airwallex_secret: {error_line(exc, region)}", file=sys.stderr)
        return 1
    print(f"seeded {secret_id} in {region} (version {result.get('VersionId', 'unknown')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
