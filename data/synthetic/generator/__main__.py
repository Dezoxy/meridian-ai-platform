"""Command line: ``python -m generator [--seed N] [--out DIR]``."""

import argparse
from collections.abc import Sequence
from pathlib import Path

from .catalogue import DEFAULT_SEED
from .output import write_dataset
from .scenarios import build_dataset

# data/synthetic, the parent of this package; independent of the working directory.
DEFAULT_OUT = Path(__file__).resolve().parent.parent


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m generator",
        description="Write the synthetic data and golden set of Meridian Insurance.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help=f"random seed (default {DEFAULT_SEED}; any other seed needs --out)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="output directory (default: the committed folder, data/synthetic)",
    )
    args = parser.parse_args(argv)
    if args.seed != DEFAULT_SEED and args.out is None:
        parser.error(
            f"--seed {args.seed} is not the committed seed ({DEFAULT_SEED}); "
            "pass --out DIR so the committed golden set is not overwritten"
        )
    out = DEFAULT_OUT if args.out is None else args.out
    dataset = build_dataset(args.seed)
    written = write_dataset(dataset, args.seed, out)
    print(f"wrote {len(written)} files to {out} (seed {args.seed})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
