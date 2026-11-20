"""python -m cmdbsync --db cmdb.sqlite --type AWS::EC2::Instance [--dry-run]"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from cmdbsync.sources import AwsConfigSource
from cmdbsync.store import SqliteSink, SyncRefused

EXIT_OK, EXIT_ERROR, EXIT_REFUSED = 0, 1, 3


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sync AWS Config inventory into a SQLite CMDB.")
    p.add_argument("--db", type=Path, default=Path("cmdb.sqlite"))
    p.add_argument(
        "--type",
        action="append",
        dest="types",
        required=True,
        help="Config resource type, e.g. AWS::EC2::Instance (repeatable)",
    )
    p.add_argument("--region", help="AWS region for the Config API")
    p.add_argument("--dry-run", action="store_true", help="print the diff, write nothing")
    p.add_argument(
        "--allow-empty-source",
        action="store_true",
        help="allow retiring everything when the source returns nothing",
    )
    return p.parse_args(argv)


def main(argv=None, config_client=None, now=None, out=print) -> int:
    args = parse_args(argv)
    try:
        if config_client is None:
            import boto3

            config_client = boto3.client("config", region_name=args.region)
        records = AwsConfigSource(config_client, args.types).fetch()
        sink = SqliteSink(args.db)
        plan = sink.plan(records, args.allow_empty_source)
        for line in plan.lines():
            out(line)
        out(f"{len(records)} resources from AWS Config; {plan.summary()}")
        if args.dry_run:
            out("dry-run: database not changed")
        elif plan.empty:
            out("nothing to do")
        else:
            sink.apply(plan, now or datetime.now(UTC))
            out(f"applied to {args.db}")
        return EXIT_OK
    except Exception as exc:
        code = EXIT_REFUSED if isinstance(exc, SyncRefused) else EXIT_ERROR
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return code
