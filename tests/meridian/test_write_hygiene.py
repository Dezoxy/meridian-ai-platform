"""S071 L3, B-4 and B-6: what a paid run may write, and how.

The checks refuse a recording or a report that holds a shape only a leak would
put there, naming the file and the kind and never the text; the writer puts a
run's files in place all together or not at all. No database and no network;
every file is under ``tmp_path``.
"""

import json
import subprocess
from pathlib import Path

import pytest
import writehygienesupport
from servicesupport import REPO_ROOT
from writehygienesupport import (
    KINDS,
    MAX_ENTRY_CHARS,
    SAMPLES,
    UnsafeFile,
    refuse_identifier_shapes,
    refuse_large_entries,
    write_all_or_none,
)

from meridian.platform.gateway.providers.recorded import (
    RecordedAnswer,
    Recording,
    load_recording,
)

ORDINARY = (
    "The description says the damage came from wear and tear over several years, "
    "so exclusion 4.2 applies. The bearer of the policy is the insured. "
    "Contact the adjuster by phone. Amount EUR 1,250; date 2026-09-01; "
    "claim CLM-0026 on policy POL-0025: 12-34-56 is not an identifier."
)
GUARDED = (REPO_ROOT / "data" / "evaluation", REPO_ROOT / "config" / "registry")


def recording_with(text: str) -> Recording:
    return Recording(
        format=1,
        recorded_for={"claims-triage": "0" * 64},
        entries={
            "a" * 64: RecordedAnswer(
                text=text,
                finish_reason="stop",
                model="fake-model",
                input_tokens=1,
                output_tokens=1,
                latency_ms=1,
            )
        },
    )


def test_every_kind_has_a_sample_and_the_sample_is_found() -> None:
    assert set(SAMPLES) == set(KINDS)
    for kind, sample in SAMPLES.items():
        assert KINDS[kind].search(sample), kind


@pytest.mark.parametrize("kind", sorted(SAMPLES))
def test_a_file_that_holds_a_kind_is_refused_naming_the_file_and_the_kind(
    kind: str,
) -> None:
    with pytest.raises(UnsafeFile) as raised:
        refuse_identifier_shapes("a-report.json", f'{{"text": "{SAMPLES[kind]}"}}')

    message = str(raised.value)
    assert message.startswith("nothing was written: a-report.json holds ")
    assert kind in message
    # The kind, never the text: no part of the sample is in the message.
    for fragment in SAMPLES[kind].split():
        if len(fragment) > 12:
            assert fragment not in message


def test_the_message_names_each_kind_found_and_still_no_text() -> None:
    text = f"{SAMPLES['a GUID shape']} {SAMPLES['an e-mail address']}"

    with pytest.raises(UnsafeFile) as raised:
        refuse_identifier_shapes("r.json", text)

    assert "a GUID shape, an e-mail address" in str(raised.value)
    assert "someone@example.org" not in str(raised.value)


def test_an_ordinary_answer_passes() -> None:
    refuse_identifier_shapes("r.json", json.dumps({"text": ORDINARY}))


def test_what_is_committed_under_the_evaluation_directory_passes() -> None:
    """The committed recording, baselines and reports: if one of them held a
    shape, the rule would refuse an honest run's ordinary text."""
    checked = 0
    for path in sorted((REPO_ROOT / "data" / "evaluation").rglob("*.json")):
        refuse_identifier_shapes(path.name, path.read_text(encoding="utf-8"))
        checked += 1
    assert checked >= 3
    recording = REPO_ROOT / "data" / "evaluation" / "recordings" / "claims-triage.json"
    refuse_large_entries(recording.name, load_recording(recording))


def test_an_entry_over_the_cap_is_refused_and_one_at_the_cap_passes() -> None:
    refuse_large_entries("rec.json", recording_with("x" * MAX_ENTRY_CHARS))

    with pytest.raises(UnsafeFile, match="holds 1 entries over 4000 characters"):
        refuse_large_entries("rec.json", recording_with("x" * (MAX_ENTRY_CHARS + 1)))


# ── all or none ─────────────────────────────────────────────────────────────
FILES = {
    "recordings/one.json": "one\n",
    "two.json": "two\n",
    "three.md": "three\n",
}


def test_the_files_are_put_in_place_and_nothing_is_left_beside_them(
    tmp_path: Path,
) -> None:
    paths = write_all_or_none(tmp_path / "out", FILES)

    assert [p.relative_to(tmp_path / "out").as_posix() for p in paths] == list(FILES)
    for relative, text in FILES.items():
        assert (tmp_path / "out" / relative).read_text(encoding="utf-8") == text
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == [
        "recordings",
        "three.md",
        "two.json",
    ]


def test_a_writer_killed_between_two_files_leaves_none_of_the_three_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    written: list[str] = []
    real = writehygienesupport._write_text

    def second_write_raises(path: Path, text: str) -> None:
        if len(written) == 1:
            raise OSError("the disk is full")
        written.append(path.name)
        real(path, text)

    monkeypatch.setattr(writehygienesupport, "_write_text", second_write_raises)
    out = tmp_path / "out"
    out.mkdir()
    (out / "two.json").write_text("older\n", encoding="utf-8")

    with pytest.raises(OSError, match="disk is full"):
        write_all_or_none(out, FILES)

    assert len(written) == 1  # the first was written, to the staging directory
    assert not (out / "recordings").exists()
    assert not (out / "three.md").exists()
    # A file that was already there is the older one, not half of the new run.
    assert (out / "two.json").read_text(encoding="utf-8") == "older\n"
    # The staging directory is gone too.
    assert sorted(p.name for p in out.iterdir()) == ["two.json"]


def test_the_staging_names_a_hard_kill_leaves_are_ignored_by_git() -> None:
    """A kill between the staging write and the removal leaves a dot-directory
    under the target; ``git add -A`` must not pick it up with the model's text in
    it. The names are checked where a run writes: ``data/evaluation``."""
    base = REPO_ROOT / "data" / "evaluation"
    names = [
        base / ".injection-run-abc123.tmp",
        base / ".injection-run-abc123.tmp" / "0-claims-triage-injection.json",
        base / ".claims-triage-injection-live.json.xyz.tmp",
        base / "recordings" / ".claims-triage-injection.json.xyz.tmp",
    ]

    for path in names:
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", str(path)],
            cwd=REPO_ROOT,
            check=False,
        )
        assert ignored.returncode == 0, path.name
