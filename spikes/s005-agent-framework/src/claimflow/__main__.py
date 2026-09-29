"""`python -m claimflow start|resume --framework maf|langgraph ...` prints JSON.

The pause and the resume can run as two separate processes: they share only the
store directory.
"""

import argparse
import importlib
from pathlib import Path

FLOW_MODULES = {"maf": "claimflow.maf_flow", "langgraph": "claimflow.langgraph_flow"}


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

    for command in (start, resume):
        command.add_argument("--framework", choices=sorted(FLOW_MODULES), required=True)
        command.add_argument("--store-dir", type=Path, required=True)
    return parser


def main() -> None:
    args = _parser().parse_args()
    flow = importlib.import_module(FLOW_MODULES[args.framework])
    if args.command == "start":
        result = flow.start(args.claim_id, args.store_dir)
    else:
        # Omitted flags are left out of the payload, so a malformed one can be sent.
        payload = {
            key: value
            for key, value in {
                "decision": args.decision,
                "adjuster_id": args.adjuster_id,
            }.items()
            if value is not None
        }
        result = flow.resume(args.run_ref, payload, args.store_dir)
    print(result.to_json())


if __name__ == "__main__":
    main()
