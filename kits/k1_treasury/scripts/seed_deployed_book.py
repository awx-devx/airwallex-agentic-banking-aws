"""Run kit 1's `seed_treasury_book` operator command against the DEPLOYED kit store.

A thin wrapper over awx_common.operator_commands.run_operator_command; the command itself is
awx_k1_treasury.OPERATOR_COMMANDS. `awx-deploy apply deploy/k1.yaml` runs the same command
through the same entry point; this script runs it on its own.

    uv run --all-packages python kits/k1_treasury/scripts/seed_deployed_book.py \\
        --ledger-table <the AwxSpine ledger table name> \\
        --map ob-supplier-eur=<poc-i1- beneficiary id> [--map ...] \\
        --book-version demo-1 [--replace] [--scenario-start YYYY-MM-DD]

Without --scenario-start the demo's dates start at the seed time, rounded up to the next hour.

Stores: awx_spine_aws.kit_store.DynamoDbKitStore (kit k1, AWS credentials from the
environment). Reads: the sandbox client (its key is loaded by the client and never printed).
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime


def main(argv: list[str] | None = None) -> int:
    from awx_common.operator_commands import OperatorContext, run_operator_command
    from awx_k1_treasury import OPERATOR_COMMANDS
    from awx_spine import AirwallexClient, EnvFileCredentials
    from awx_spine_aws.kit_store import DynamoDbKitStore

    parser = argparse.ArgumentParser(description="Seed kit 1's treasury book (deployed).")
    parser.add_argument("--ledger-table", required=True)
    parser.add_argument("--map", action="append", default=[], metavar="OBLIGATION=BENEFICIARY")
    parser.add_argument("--book-version", required=True)
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--scenario-start", help="ISO date (midnight UTC) or date-time")
    args = parser.parse_args(argv)

    def store_for(tenant_id: str) -> DynamoDbKitStore:
        return DynamoDbKitStore(args.ledger_table, kit="k1", tenant_id=tenant_id)

    ctx = OperatorContext(
        kit="k1",
        platform_store=store_for("platform"),
        store_for=store_for,
        now=datetime.now(UTC),
        read=AirwallexClient(EnvFileCredentials()).read,
    )
    report = run_operator_command(
        OPERATOR_COMMANDS,
        "seed_treasury_book",
        ctx,
        {
            "beneficiaries": dict(item.split("=", 1) for item in args.map),
            "book_version": args.book_version,
            "replace": args.replace,
            **({"scenario_start": args.scenario_start} if args.scenario_start else {}),
        },
    )
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
