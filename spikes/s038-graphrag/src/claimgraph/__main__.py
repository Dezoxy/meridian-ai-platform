"""Print the census of the committed data and of the simulated variant as JSON,
one object each, labelled. The numbers in the README are this output.

    PYTHONPATH=spikes/s038-graphrag/src uv run python -m claimgraph
"""

import json
import sys
from pathlib import Path

from .build import build_graph
from .census import census
from .variant import build_variant

DATA_DIR = Path(__file__).resolve().parents[4] / "data" / "synthetic"


def main() -> None:
    for graph in (build_graph(DATA_DIR), build_variant(DATA_DIR)):
        sys.stdout.write(json.dumps(census(graph), indent=1, sort_keys=False) + "\n")


if __name__ == "__main__":
    main()
