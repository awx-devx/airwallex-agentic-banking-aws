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
file, else the AirwallexSecretArn output of the AwxSpine stack in that Region.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import boto3

# The file `cdk deploy --outputs-file cdk-outputs.json` writes when run from infra/.
DEFAULT_OUTPUTS = Path(__file__).resolve().parents[1] / "infra" / "cdk-outputs.json"
SPINE_STACK = "AwxSpine"
ARN_OUTPUT = "AirwallexSecretArn"
CLIENT_ID_VAR = "AWX_CLIENT_ID"
API_KEY_VAR = "AWX_API_KEY"


class SeedError(Exception):
    """A refusal, reported as one line with no credential value in it."""


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
        help="the secret's ARN or name (default: AwxSpine.AirwallexSecretArn)",
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
        raise SeedError(f"{path}: not valid JSON ({exc.msg})") from None
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


def resolve_secret_id(
    secret_id: str | None,
    outputs: Mapping[str, Any],
    region: str,
    session: Any,
    path: Path,
) -> str:
    """--secret-id, else the outputs file's ARN, else the spine stack's output in `region`."""
    if secret_id and secret_id.strip():
        return secret_id.strip()
    arn = _output(outputs, SPINE_STACK, ARN_OUTPUT)
    if arn:
        recorded = _arn_region(arn)
        if recorded != region:
            raise SeedError(
                f"{path} holds the secret of a deployment in {recorded or 'an unknown Region'}, "
                f"not {region}; pass --region {recorded} for that deployment, or --secret-id"
            )
        return arn
    # The outputs file is written once the whole deploy has finished; the spine stack's
    # outputs exist as soon as it has deployed.
    stacks = session.client("cloudformation").describe_stacks(StackName=SPINE_STACK)["Stacks"]
    for output in stacks[0].get("Outputs", []) if stacks else []:
        if output.get("OutputKey") == ARN_OUTPUT and output.get("OutputValue"):
            return str(output["OutputValue"])
    raise SeedError(f"stack {SPINE_STACK} in {region} has no {ARN_OUTPUT} output")


def _has_terminal() -> bool:
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    except OSError:
        return False
    os.close(fd)
    return True


def read_credentials(
    from_env: bool,
    environ: Mapping[str, str],
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
    else:
        if not has_terminal():
            raise SeedError("no terminal to prompt on; pass --from-env in a shell with no terminal")
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
    values: dict[str, str] = {}
    try:
        outputs = read_outputs(args.outputs)
        region = resolve_region(args.region, outputs, args.outputs)
        session = session_factory(profile_name=args.profile, region_name=region)
        secret_id = resolve_secret_id(args.secret_id, outputs, region, session, args.outputs)
        values = read_credentials(
            args.from_env, os.environ if environ is None else environ, prompt, has_terminal
        )
        result = session.client("secretsmanager").put_secret_value(
            SecretId=secret_id, SecretString=json.dumps(values)
        )
    except (KeyboardInterrupt, EOFError):
        print("\ncancelled; nothing was uploaded", file=sys.stderr)
        return 130
    except SeedError as exc:
        print(f"seed_airwallex_secret: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        # An AWS error names the secret and the operation. Any value it held is masked anyway.
        message = f"{type(exc).__name__}: {exc}"
        for value in values.values():
            message = message.replace(value, "***")
        print(f"seed_airwallex_secret: {message}", file=sys.stderr)
        return 1
    print(f"seeded {secret_id} in {region} (version {result.get('VersionId', 'unknown')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
