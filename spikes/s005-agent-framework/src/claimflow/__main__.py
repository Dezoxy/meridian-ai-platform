"""`python -m claimflow start|resume --framework maf|langgraph ...` prints JSON.

The pause and the resume can run as two separate processes: they share only the
store directory. `resume` takes `--decision` and `--adjuster-id`, or, to send
malformed input on purpose, `--raw-payload '<json>'`; never a silent mix.
"""

import argparse
import importlib
import json
from pathlib import Path
from typing import Any

FLOW_MODULES = {"maf": "claimflow.maf_flow", "langgraph": "claimflow.langgraph_flow"}
MAF_STORES = ["file", "json"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="claimflow")
    commands = parser.add_subparsers(dest="command", required=True)

    start = commands.add_parser(
        "start", help="run a claim until it completes or pauses"
    )
    start.add_argument("--claim-id", required=True)

    resume = commands.add_parser("resume", help="deliver an adjuster decision")
    resume.add_argument("--run-ref", required=True)
    resume.add_argument("--decision")
    resume.add_argument("--adjuster-id")
    resume.add_argument(
        "--raw-payload",
        help="a JSON value sent as is, instead of --decision and --adjuster-id",
    )

    for command in (start, resume):
        command.add_argument("--framework", choices=sorted(FLOW_MODULES), required=True)
        command.add_argument("--store-dir", type=Path, required=True)
        command.add_argument(
            "--maf-store",
            choices=MAF_STORES,
            help="checkpoint store for maf: pickle-based 'file' (default) or 'json'",
        )
    return parser


def _payload(parser: argparse.ArgumentParser, args: argparse.Namespace) -> Any:
    """The resume payload: both flags, or the raw JSON, and never just one."""
    if args.raw_payload is not None:
        if args.decision is not None or args.adjuster_id is not None:
            parser.error("--raw-payload excludes --decision and --adjuster-id")
        try:
            return json.loads(args.raw_payload)
        except json.JSONDecodeError as error:
            parser.error(f"--raw-payload is not JSON: {error}")
    if args.decision is None or args.adjuster_id is None:
        parser.error("resume needs --decision and --adjuster-id (or --raw-payload)")
    return {"decision": args.decision, "adjuster_id": args.adjuster_id}


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    flow = importlib.import_module(FLOW_MODULES[args.framework])
    options: dict[str, Any] = {}
    if args.maf_store is not None:
        if args.framework != "maf":
            parser.error("--maf-store applies to --framework maf only")
        options["store"] = args.maf_store
    if args.command == "start":
        result = flow.start(args.claim_id, args.store_dir, **options)
    else:
        payload = _payload(parser, args)
        result = flow.resume(args.run_ref, payload, args.store_dir, **options)
    print(result.to_json())


if __name__ == "__main__":
    main()
