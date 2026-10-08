"""The judge against a person's labels (S071, decision E7).

``python -m meridian.workloads.claims_triage.judge_labels sheet`` writes the
worksheet a person labels blind: for each judge verdict in the recording, the
claim, the rationale the judge read and the clauses it was shown, with an empty
``person_grounded``. It holds neither the judge's verdict nor its reason.
``... compare`` reads the filled sheet and the baseline and prints how far the
two agree.

It lives in the workload, not in ``meridian.platform.evaluation``: the clauses
the judge was shown are the rules' candidate exclusions (``select_terms``), and
the platform never imports a workload. It reads committed files only, calls no
model and needs no database.

The worksheet is a person's, not a fingerprint: ``make eval`` does not read it.
Output names claim IDs and verdicts, and the judge's recorded reason; it never
prints a rationale, a description or a clause (the evaluation's rule).
"""

import argparse
import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from meridian.platform.common.jsonfile import JsonFileError, read_json_file
from meridian.platform.evaluation.judge import read_answer
from meridian.platform.knowledge_mcp.chunking import parse_wording

from .wording import Clause, select_terms

ROOT = Path(__file__).resolve().parents[4]
SYNTHETIC = ROOT / "data" / "synthetic"
EVALUATION = ROOT / "data" / "evaluation"
BASELINE = EVALUATION / "claims-triage-baseline.json"
RECORDING = EVALUATION / "recordings" / "claims-triage.json"
SHEET = EVALUATION / "judge-labels" / "claims-triage.json"

SHEET_FORMAT = 1
EXIT_CLEAN = 0
EXIT_REFUSED = 2
CLAIM_ID = re.compile(r"CLM-[0-9]{4}")
# A product names a wording file: only the shape of a product may.
PRODUCT = re.compile(r"[A-Za-z0-9-]+")
JUDGED = "grounded"
PERSON_FIELDS = ("person_grounded", "person_note")

INSTRUCTIONS = (
    "Read each entry's rationale together with the claim and the clauses "
    "shown, as the judge did. "
    "Set person_grounded to true when every statement of the rationale is "
    "supported by the claim and the clauses shown, and to false otherwise. "
    "Leave a note in person_note where the call was close, and do not open "
    "the baseline or the recording until every entry is labelled."
)


class SheetError(ValueError):
    """A refusal. The message is fixed text and names at most a claim ID that
    has the shape of one: a file's own text is never quoted."""


def _read(path: Path) -> Any:
    try:
        return read_json_file(path)
    except JsonFileError as error:
        raise SheetError(f"{path.name}: {error}") from None


def _judged_cases(baseline: Any) -> dict[str, Mapping[str, Any]]:
    """The baseline's cases the judge was asked about, by claim ID: each has a
    ``judge_reason``."""
    cases = baseline.get("cases") if isinstance(baseline, dict) else None
    if not isinstance(cases, list):
        raise SheetError("the baseline holds no list of cases")
    judged: dict[str, Mapping[str, Any]] = {}
    for case in cases:
        observed = case.get("observed") if isinstance(case, dict) else None
        if not isinstance(observed, dict):
            raise SheetError("a baseline case has no observed answer")
        if observed.get("judge_reason") is None:
            continue
        claim_id = case.get("case")
        if not isinstance(claim_id, str) or not CLAIM_ID.fullmatch(claim_id):
            raise SheetError("a baseline case has no valid claim ID")
        if claim_id in judged:
            raise SheetError(f"{claim_id} is in the baseline twice")
        judged[claim_id] = observed
    return judged


def _recorded_reasons(recording: Any) -> list[str]:
    """The reason of each judge verdict in the recording, in its order. A
    recorded answer is the judge's when it reads as the judge's format (the
    triage's answers do not)."""
    entries = recording.get("entries") if isinstance(recording, dict) else None
    if not isinstance(entries, dict):
        raise SheetError("the recording holds no entries")
    reasons: list[str] = []
    for answer in entries.values():
        if not isinstance(answer, dict):
            raise SheetError("a recorded answer is not an object")
        text, finish = answer.get("text"), answer.get("finish_reason")
        if not isinstance(text, str) or not isinstance(finish, str):
            raise SheetError("a recorded answer has no text or finish reason")
        reason = read_answer(text, finish).reason
        if reason is not None:
            reasons.append(reason)
    return reasons


def _claims_in_recording_order(
    judged: Mapping[str, Mapping[str, Any]], reasons: Sequence[str]
) -> list[str]:
    """The claim ID of each recorded verdict. The recording keys an answer by
    the hash of its request and names no claim, so the link is the reason: the
    one the baseline kept for that claim. A verdict that matches no case or two,
    or a judged case with no verdict, is refused."""
    by_reason: dict[str, str] = {}
    for claim_id, observed in judged.items():
        reason = observed["judge_reason"]
        if reason in by_reason:
            raise SheetError("two baseline cases hold the same judge reason")
        by_reason[reason] = claim_id
    order = []
    for reason in reasons:
        if reason not in by_reason:
            raise SheetError("a recorded verdict matches no baseline case")
        order.append(by_reason[reason])
    if len(set(order)) != len(order):
        raise SheetError("two recorded verdicts match one baseline case")
    if set(order) != set(judged):
        raise SheetError("a baseline case has no recorded verdict")
    return order


def _records(synthetic: Path, name: str, key: str) -> dict[str, dict[str, Any]]:
    document = _read(synthetic / name)
    if not isinstance(document, list):
        raise SheetError(f"{name} is not a list of records")
    found: dict[str, dict[str, Any]] = {}
    for record in document:
        if not isinstance(record, dict) or not isinstance(record.get(key), str):
            raise SheetError(f"{name} holds a record with no {key}")
        found[record[key]] = record
    return found


def _clauses_shown(
    claim: Mapping[str, Any], policy: Mapping[str, Any], synthetic: Path
) -> tuple[Clause, ...]:
    """The clauses the judge was shown for a claim: the candidate exclusions the
    rules pick from the whole wording of the policy's product (the same read as
    the evaluation's ``candidates_of``, which a test holds equal)."""
    product = policy["product"]
    if not isinstance(product, str) or not PRODUCT.fullmatch(product):
        raise SheetError("a policy names a product that is not a product name")
    wording = synthetic / "wordings" / f"{product}.md"
    try:
        text = wording.read_text(encoding="utf-8")
    except OSError:
        raise SheetError("a policy's wording cannot be read") from None
    chunks = [
        {"clause": c.clause, "section": c.section, "title": c.title, "body": c.body}
        for c in parse_wording(text).chunks
    ]
    return select_terms(
        claim["peril"],
        chunks,
        product=product,
        wording_version=policy["wording_version"],
    ).candidates


def _entry(
    claim_id: str,
    observed: Mapping[str, Any],
    claim: Mapping[str, Any],
    clauses: Sequence[Clause],
) -> dict[str, Any]:
    return {
        "claim_id": claim_id,
        "peril": claim["peril"],
        "description": claim["description"],
        "assessment": observed["assessment"],
        "rationale": observed["rationale"],
        "clauses": [
            {"clause": c.clause, "title": c.title, "text": c.body} for c in clauses
        ],
        "person_grounded": None,
        "person_note": "",
    }


def build_sheet(baseline: Path, recording: Path, synthetic: Path) -> dict[str, Any]:
    """The worksheet, from committed files only."""
    judged = _judged_cases(_read(baseline))
    order = _claims_in_recording_order(judged, _recorded_reasons(_read(recording)))
    claims = _records(synthetic, "claims.json", "claim_id")
    policies = _records(synthetic, "policies.json", "policy_number")
    entries = []
    for claim_id in order:
        claim = claims.get(claim_id)
        policy = policies.get(claim["policy_number"]) if claim else None
        if claim is None or policy is None:
            raise SheetError(f"the golden set has no claim and policy for {claim_id}")
        clauses = _clauses_shown(claim, policy, synthetic)
        entries.append(_entry(claim_id, judged[claim_id], claim, clauses))
    return {"format": SHEET_FORMAT, "instructions": INSTRUCTIONS, "entries": entries}


def dump_sheet(sheet: Mapping[str, Any]) -> str:
    return json.dumps(sheet, indent=2, ensure_ascii=False) + "\n"


def _begun(path: Path) -> bool:
    """Whether the sheet at ``path`` holds a label or a note: writing over it
    would destroy the person's work. A file that cannot be read as a sheet is
    not overwritten either: nothing says it holds no work."""
    if not path.exists():
        return False
    document = _read(path)
    entries = document.get("entries") if isinstance(document, dict) else None
    if not isinstance(entries, list):
        return False
    return any(
        isinstance(entry, dict)
        and (entry.get("person_grounded") is not None or entry.get("person_note"))
        for entry in entries
    )


def write_sheet(args: argparse.Namespace) -> None:
    sheet = build_sheet(args.baseline, args.recording, args.synthetic)
    if _begun(args.out):
        raise SheetError(
            f"{args.out.name} already holds labels or notes; "
            "move it aside to write a new sheet"
        )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(dump_sheet(sheet), encoding="utf-8", newline="\n")
    print(f"wrote {len(sheet['entries'])} entries to {args.out.name}")


def _labels(sheet: Any, judged: Mapping[str, Mapping[str, Any]]) -> dict[str, bool]:
    """The labels the person filled, by claim ID. Nothing is read until every
    entry has been checked: a label that is not a boolean, an ID twice or an ID
    the judge was not asked about refuses the sheet."""
    entries = sheet.get("entries") if isinstance(sheet, dict) else None
    if not isinstance(entries, list):
        raise SheetError("the sheet holds no list of entries")
    seen: set[str] = set()
    labels: dict[str, bool] = {}
    for entry in entries:
        claim_id = entry.get("claim_id") if isinstance(entry, dict) else None
        if not isinstance(claim_id, str) or not CLAIM_ID.fullmatch(claim_id):
            raise SheetError("an entry has no valid claim ID")
        if claim_id in seen:
            raise SheetError(f"{claim_id} is on the sheet twice")
        seen.add(claim_id)
        if claim_id not in judged:
            raise SheetError(f"{claim_id} is not a claim the judge was asked about")
        label = entry.get("person_grounded")
        if label is not None and not isinstance(label, bool):
            raise SheetError(f"{claim_id}: person_grounded must be true, false or null")
        if label is not None:
            labels[claim_id] = label
    return labels


def _word(grounded: bool) -> str:
    return "grounded" if grounded else "ungrounded"


def _one_line(text: str) -> str:
    return " ".join(text.split())


def compare_labels(args: argparse.Namespace) -> None:
    judged = _judged_cases(_read(args.baseline))
    labels = _labels(_read(args.labels), judged)
    total = len(judged)
    if not labels:
        print(f"no label is filled in: nothing to compare ({total} entries wait)")
        return
    verdict = {c: o.get("groundedness") == JUDGED for c, o in judged.items()}
    matching = [c for c in sorted(labels) if labels[c] == verdict[c]]
    differing = [c for c in sorted(labels) if labels[c] != verdict[c]]
    print(f"labelled: {len(labels)} of {total}")
    print(f"missing: {total - len(labels)}")
    share = 100 * len(matching) / len(labels)
    print(f"agreement: {len(matching)} of {len(labels)} ({share:.1f}%)")
    if not differing:
        print("disagreements: none")
    else:
        print("disagreements:")
        for claim_id in differing:
            print(
                f"  {claim_id}: person: {_word(labels[claim_id])}, "
                f"judge: {_word(verdict[claim_id])}"
            )
            print(f"    judge's reason: {_one_line(judged[claim_id]['judge_reason'])}")
    print("confusion:")
    for person in (True, False):
        for judge in (True, False):
            count = sum(
                1 for c in labels if labels[c] is person and verdict[c] is judge
            )
            print(f"  person {_word(person)}, judge {_word(judge)}: {count}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="judge_labels", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    sheet = commands.add_parser("sheet", help="write the worksheet to label")
    sheet.add_argument("--baseline", type=Path, default=BASELINE)
    sheet.add_argument("--recording", type=Path, default=RECORDING)
    sheet.add_argument("--synthetic", type=Path, default=SYNTHETIC)
    sheet.add_argument("--out", type=Path, default=SHEET)
    sheet.set_defaults(run=write_sheet)
    compare = commands.add_parser("compare", help="compare the labels with the judge")
    compare.add_argument("--labels", type=Path, default=SHEET)
    compare.add_argument("--baseline", type=Path, default=BASELINE)
    compare.set_defaults(run=compare_labels)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        args.run(args)
    except SheetError as error:
        print(f"judge_labels: {error}", file=sys.stderr)
        return EXIT_REFUSED
    return EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
