"""The injection report carries the screens' fingerprint (S061).

The committed case set and manifests are the input; no case is graded for what
it says, so no text a case adds is read here.
"""

from servicesupport import REGISTRY_DIR, REPO_ROOT

from meridian.platform.evaluation.report import AnsweredBy
from meridian.platform.guardrails import screen_fingerprint
from meridian.platform.registry import load_registry
from meridian.workloads.claims_triage.injection import (
    Outcome,
    build_injection_report,
    load_cases,
)

SYNTHETIC = REPO_ROOT / "data" / "synthetic"
INJECTION = SYNTHETIC / "injection"
SCRIPTED = AnsweredBy(kind="scripted", label="simulated")
PROMPT = "ab" * 32


def test_the_injection_report_carries_the_fingerprint_of_the_screens() -> None:
    cases = load_cases(INJECTION / "cases.json")
    outcomes = {case.case: Outcome(None, None, ()) for case in cases}
    expected = {
        case.base_claim: {"route": "adjuster", "recommendation": None} for case in cases
    }

    report = build_injection_report(
        cases,
        outcomes,
        expected,
        manifest_path=INJECTION / "manifest.json",
        golden_manifest_path=SYNTHETIC / "manifest.json",
        registry=load_registry(REGISTRY_DIR),
        answered_by=SCRIPTED,
        prompt=PROMPT,
    )

    assert report.fingerprints.screen == screen_fingerprint()
