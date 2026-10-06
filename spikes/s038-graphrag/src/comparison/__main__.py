"""Print the README's tables from the committed results file.

PYTHONPATH=spikes/s038-graphrag/src uv run python -m comparison
"""

import sys

from .tables import load, render


def main() -> None:
    sys.stdout.write(render(load()) + "\n")


if __name__ == "__main__":
    main()
