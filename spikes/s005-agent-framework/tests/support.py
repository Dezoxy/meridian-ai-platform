"""Constants and helpers shared by the test modules (imported via pythonpath)."""

import json
import os
import subprocess
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
FRAMEWORKS = ["maf", "langgraph"]
FLOW_MODULES = {"maf": "claimflow.maf_flow", "langgraph": "claimflow.langgraph_flow"}

# Golden-set claims picked from data/synthetic/claims.json. test_rules.py pins
# each pick, so a regenerated data set fails loudly instead of silently.
AUTO_APPROVE_CLAIM = "CLM-0005"  # 1220 EUR, active in-date policy
OVER_THRESHOLD_CLAIM = "CLM-0004"  # 2970 EUR, active in-date policy
BOUNDARY_CLAIM = "CLM-0007"  # 2501 EUR, one euro over the threshold
LAPSED_POLICY_CLAIM = "CLM-0002"  # policy status lapsed
OUT_OF_DATE_CLAIM = "CLM-0014"  # policy active, loss date outside its term


def run_cli(*args: str) -> dict:
    """Run `python -m claimflow ...` in its own process and parse its JSON."""
    completed = subprocess.run(
        [sys.executable, "-m", "claimflow", *args],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(SRC_DIR)},
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)
