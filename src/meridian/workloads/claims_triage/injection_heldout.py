"""Measure the injection screen on the held-out cases (S071).

``python -m meridian.workloads.claims_triage.injection_heldout`` reads the 72
held-out cases (``data/synthetic/injection-heldout/``), applies the screen to each
case's description as the service does, and writes two files under
``data/evaluation/injection-heldout/``: ``injection-heldout.json`` (per case:
its ID, the writer's ID, label, family, language, base claim and the screen's
outcome) and ``injection-heldout-summary.md``. It needs no model, no database
and no cluster, and it imports neither the Claims API nor LangGraph (the
platform never imports a workload, so the command lives here, as ``sweep``
does).

The outcome is read the way the service reads a description: the Claims API
screens the description as posted (``posted_text``), and the assessor screens the
run's copy, with the claimant's name replaced (``claimant_name``), asking the
special-category screen first. So ``special-data`` is a description the
special-category screen took before the injection screen was asked,
``injection-suspected`` one the injection screen stopped, and ``none`` one that
passed both. Only ``injection-suspected`` counts as stopped (an attack) or as
flagged (a look-alike), as the existing grader counts it; the ``special-data``
cases are listed apart by ID. The screen runs only where the assessor is reached,
which needs a candidate exclusion clause; every base claim of the injection set's
description cases reaches it (its baseline shows each as flagged or asked).

The held-out set is a report, not a fingerprint of the gate: ``make eval`` does
not read it. It was measured once and is not tuned on: the moment the screen's
patterns are changed after reading its result it is spent, and a new blind set is
needed. A report holds the cases' IDs, labels, families and languages, never a
sentence or a description, and never the writer's note on what a sentence is
after (``data/evaluation/README.md``).
"""

import argparse
import hashlib
import json
import re
import sys
import textwrap
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meridian.platform.evaluation.report import ReportError
from meridian.platform.guardrails import addresses_the_model, holds_special_category

from .claimant_name import description_for_run
from .evaluation import screens_fingerprint
from .injection import InjectionCase, _percent, load_cases
from .models import Claimant, ClaimSubmission
from .posted_text import posted_text_addresses_the_model

# src/meridian/workloads/claims_triage/injection_heldout.py: the checkout is the
# fourth parent. The command is a development tool and reads the checkout's data.
ROOT = Path(__file__).resolve().parents[4]
CASES_PATH = ROOT / "data" / "synthetic" / "injection-heldout" / "cases.json"
MANIFEST_PATH = ROOT / "data" / "synthetic" / "injection-heldout" / "manifest.json"
EVALUATION_DIR = ROOT / "data" / "evaluation"
# A folder of its own, not data/evaluation itself: a test loads every JSON file
# directly under data/evaluation as a gate report, and this is not one.
DEFAULT_OUT = EVALUATION_DIR / "injection-heldout"
REPORT_NAME = "injection-heldout.json"
SUMMARY_NAME = "injection-heldout-summary.md"
EXISTING_SUMMARY_NAME = "injection-summary.md"
SPECIAL_DATA = "special-data"
INJECTION_SUSPECTED = "injection-suspected"
NONE = "none"
ATTACK, BENIGN = "attack", "benign"
SUMMARY_WIDTH = 80
EXIT_OK, EXIT_REFUSED = 0, 1
NONE_LISTED = "none"


def screen_outcome(description: str, claimant: Claimant) -> str:
    """What the screens do with a description as the service reads it: the
    special-category screen first, on the run's copy; then the injection screen,
    on the description as posted (the Claims API's) or on the run's copy (the
    assessor's own)."""
    run_copy = description_for_run(description, claimant)
    if holds_special_category(run_copy):
        return SPECIAL_DATA
    if posted_text_addresses_the_model(description) or addresses_the_model(run_copy):
        return INJECTION_SUSPECTED
    return NONE


@dataclass(frozen=True, slots=True)
class ExistingRates:
    """The existing injection set's two rates and its description attacks, as its
    committed summary states them."""

    attacks: int
    attacks_stopped: int
    benign: int
    benign_flagged: int
    description_attacks: int
    description_attacks_stopped: int


def read_existing_summary(text: str) -> ExistingRates:
    """Read the numbers out of the committed ``injection-summary.md``; they are
    quoted, not recomputed. ``ValueError`` when the bullets are not there."""
    flat = " ".join(text.split())
    attacks = re.search(r"- Attacks: (\d+); stopped before the model: (\d+) ", flat)
    benign = re.search(r"- Benign cases: (\d+); flagged by the screen: (\d+) ", flat)
    if attacks is None or benign is None:
        raise ValueError("injection-summary.md does not state its two rates")
    rows = re.findall(r"\| description \| [a-z-]+ \| (\d+) \| (\d+) \|", text)
    return ExistingRates(
        attacks=int(attacks[1]),
        attacks_stopped=int(attacks[2]),
        benign=int(benign[1]),
        benign_flagged=int(benign[2]),
        description_attacks=sum(int(cases) for cases, _ in rows),
        description_attacks_stopped=sum(int(stopped) for _, stopped in rows),
    )


def _read_manifest(manifest_path: Path, cases_path: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text("utf-8"))
    actual = hashlib.sha256(cases_path.read_bytes()).hexdigest()
    if manifest.get("files", {}).get("cases.json") != actual:
        raise ReportError("the held-out manifest does not hold the cases file's hash")
    return manifest


def measure(
    cases: Sequence[InjectionCase], manifest: Mapping[str, Any]
) -> list[dict[str, str]]:
    """One entry per case, in case order: the screen's outcome on the case's
    description, beside the labels the manifest keeps. No sentence."""
    entries = []
    for case in cases:
        submission = ClaimSubmission.model_validate_json(json.dumps(case.claim))
        kept = manifest["cases"][case.case]
        entries.append(
            {
                "case": case.case,
                "h_id": kept["h_id"],
                "label": case.label,
                "family": case.family,
                "language": kept["language"],
                "base_claim": case.base_claim,
                "outcome": screen_outcome(submission.description, submission.claimant),
            }
        )
    return entries


def render_report(entries: list[dict[str, str]], cases_sha256: str) -> str:
    """The JSON report: ASCII, byte-stable, no timestamp and no path."""
    document = {
        "synthetic": True,
        "held_out": True,
        "workload": "claims-triage",
        "cases_sha256": cases_sha256,
        "screen": screens_fingerprint(),
        "cases": entries,
    }
    return json.dumps(document, indent=2, ensure_ascii=True) + "\n"


# -- the summary ------------------------------------------------------------------
def _fill(text: str) -> str:
    return textwrap.fill(
        text,
        width=SUMMARY_WIDTH,
        subsequent_indent="  " if text.startswith("- ") else "",
        break_long_words=False,
        break_on_hyphens=False,
    )


def _ids(label: str, ids: Sequence[str]) -> str:
    return _fill(f"- {label}: {', '.join(ids) or NONE_LISTED}")


def _stopped(entry: Mapping[str, str]) -> bool:
    return entry["outcome"] == INJECTION_SUSPECTED


def _of(count: int, whole: int) -> str:
    return f"{count} of {whole} ({_percent(count, whole)})"


OPENING = _fill(
    "The injection screen was applied to 72 sentences that a writer who had "
    "read nothing of this repository wrote: 48 attacks in eight families and "
    "24 look-alikes, benign claims that quote or mention the words an attack "
    "uses. Each sentence is appended to the description of a golden claim and "
    "the description is screened as the service screens it. No model is called."
)
HOW_TO_READ = [
    _fill(
        "The set is small: 48 attacks, six to a family, and 24 look-alikes, so "
        "one case moves a rate by two or four points and a family's rate says "
        "little by itself."
    ),
    _fill(
        "The sentences were written by a model and not by an attacker, so the "
        "rates say how the screen does against what a model writes when asked "
        "for such sentences, not against someone who has read the screen."
    ),
    _fill(
        "The set is spent for tuning the moment the screen's patterns are "
        "changed after reading this result: a screen changed to catch what is "
        "listed here is no longer measured by it, and a new blind set is then "
        "needed."
    ),
]
JOIN_NOTE = _fill(
    "The existing set joins its sentences to a description in other ways too "
    "(before it, or on a new line); every held-out sentence is appended, so the "
    "two rates are not measured the same way in that respect, and the existing "
    "totals include its clause cases, which this set has none of."
)


def _family_rows(entries: Sequence[Mapping[str, str]]) -> list[str]:
    totals: dict[str, list[int]] = {}
    for entry in entries:
        if entry["label"] != ATTACK:
            continue
        counts = totals.setdefault(entry["family"], [0, 0])
        counts[0] += 1
        counts[1] += _stopped(entry)
    return [
        f"| attack | {family} | {cases} | {stopped} | {_percent(stopped, cases)} |"
        for family, (cases, stopped) in totals.items()
    ]


def _language_rows(entries: Sequence[Mapping[str, str]]) -> list[str]:
    counts: dict[str, Counter[str]] = {}
    for entry in entries:
        tally = counts.setdefault(entry["language"], Counter())
        tally[f"{entry['label']}"] += 1
        tally[f"{entry['label']}-stopped"] += _stopped(entry)
    return [
        f"| {language} | {tally[ATTACK]} | {tally[f'{ATTACK}-stopped']} "
        f"| {tally[BENIGN]} | {tally[f'{BENIGN}-stopped']} |"
        for language, tally in sorted(counts.items())
    ]


def render_summary(
    entries: Sequence[Mapping[str, str]], existing: ExistingRates
) -> str:
    """The summary as Markdown, byte-stable for equal input. IDs only: never a
    sentence."""
    attacks = [e for e in entries if e["label"] == ATTACK]
    benign = [e for e in entries if e["label"] == BENIGN]
    stopped = sum(_stopped(e) for e in attacks)
    flagged = sum(_stopped(e) for e in benign)
    special = [e["case"] for e in entries if e["outcome"] == SPECIAL_DATA]
    lines = [
        "# Injection screen on held-out sentences: claims triage",
        "",
        OPENING,
        "",
        _fill(
            f"- Attacks: {len(attacks)}; stopped by the injection screen: "
            f"{stopped} ({_percent(stopped, len(attacks))})."
        ),
        _fill(
            f"- Look-alikes: {len(benign)}; flagged by the injection screen: "
            f"{flagged} ({_percent(flagged, len(benign))})."
        ),
        f"- Taken first by the special-category screen: {len(special)}.",
        "",
        "| Label | Family | Cases | Stopped | Rate |",
        "| --- | --- | --- | --- | --- |",
        *_family_rows(entries),
        "",
        "| Language | Attacks | Stopped | Look-alikes | Flagged |",
        "| --- | --- | --- | --- | --- |",
        *_language_rows(entries),
        "",
        "| Set | Attacks stopped | Benign cases flagged |",
        "| --- | --- | --- |",
        f"| Held-out (this report) | {_of(stopped, len(attacks))} "
        f"| {_of(flagged, len(benign))} |",
        f"| Existing set, all cases (injection-summary.md) "
        f"| {_of(existing.attacks_stopped, existing.attacks)} "
        f"| {_of(existing.benign_flagged, existing.benign)} |",
        f"| Existing set, description attacks only "
        f"| {_of(existing.description_attacks_stopped, existing.description_attacks)} "
        "| not stated |",
        "",
        JOIN_NOTE,
        "",
        _ids(
            "Attacks that passed both screens",
            [e["case"] for e in attacks if e["outcome"] == NONE],
        ),
        _ids("Look-alikes flagged", [e["case"] for e in benign if _stopped(e)]),
        _ids("Taken first by the special-category screen", special),
        "",
        "## How to read the number",
        "",
        *[line for paragraph in HOW_TO_READ for line in (paragraph, "")],
    ]
    return "\n".join(lines).rstrip("\n") + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m meridian.workloads.claims_triage.injection_heldout",
        description=(
            "Apply the injection screen to the held-out cases and write the two "
            "reports. No model, no database."
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help="folder the two reports are written to "
        "(default data/evaluation/injection-heldout); "
        f"{EXISTING_SUMMARY_NAME} is always read from data/evaluation",
    )
    args = parser.parse_args(argv)
    try:
        cases = load_cases(CASES_PATH)
        manifest = _read_manifest(MANIFEST_PATH, CASES_PATH)
        existing = read_existing_summary(
            (EVALUATION_DIR / EXISTING_SUMMARY_NAME).read_text("utf-8")
        )
        entries = measure(cases, manifest)
        report = render_report(entries, manifest["files"]["cases.json"])
        summary = render_summary(entries, existing)
    except (ReportError, ValueError, OSError) as exc:
        print(f"refused: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / REPORT_NAME).write_text(report, encoding="utf-8")
    (args.out / SUMMARY_NAME).write_text(summary, encoding="utf-8")
    print(f"wrote {REPORT_NAME} and {SUMMARY_NAME} to {args.out}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
