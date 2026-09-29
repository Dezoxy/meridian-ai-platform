"""The generator is deterministic and the committed output is current."""

import hashlib
from pathlib import Path

import pytest
from generator import __main__ as cli
from generator import catalogue

OTHER_SEED = 7


def test_rerun_is_byte_identical_across_processes(
    tmp_path: Path, run_generator, read_tree
):
    # Arrange: two runs whose hash randomisation differs, so any dependence on
    # set ordering would show up as different bytes.
    first, second = tmp_path / "first", tmp_path / "second"

    # Act
    run_generator(first, hash_seed="1")
    run_generator(second, hash_seed="2")

    # Assert
    assert read_tree(first) == read_tree(second)
    assert read_tree(first), "the generator wrote nothing"


def test_a_different_seed_changes_the_claims(tmp_path: Path, run_generator, generated):
    # Arrange
    other = tmp_path / "other"

    # Act
    run_generator(other, seed=OTHER_SEED)

    # Assert
    assert (other / "claims.json").read_bytes() != generated["claims.json"]


def test_committed_output_matches_a_fresh_run(generated, synthetic_dir: Path):
    # Arrange
    def is_generated_kind(path: Path) -> bool:
        if "__pycache__" in path.parts:
            return False
        return path.suffix == ".json" or (
            path.suffix == ".md" and path.parent.name == "wordings"
        )

    committed = {
        path.relative_to(synthetic_dir).as_posix()
        for path in synthetic_dir.rglob("*")
        if path.is_file() and is_generated_kind(path)
    }

    # Act
    stale = sorted(
        path
        for path, content in generated.items()
        if not (synthetic_dir / path).is_file()
        or (synthetic_dir / path).read_bytes() != content
    )
    unknown = sorted(committed - set(generated))

    # Assert
    assert stale == [], f"run `make synthetic` and commit: {stale}"
    assert unknown == [], f"files the generator did not write: {unknown}"


def test_manifest_hashes_every_other_committed_file(
    synthetic_dir: Path, manifest: dict
):
    # Arrange
    listed = list(manifest["files"])

    # Act
    actual = {
        path: hashlib.sha256((synthetic_dir / path).read_bytes()).hexdigest()
        for path in listed
    }

    # Assert
    assert listed == sorted(listed)
    assert "manifest.json" not in listed
    assert manifest["files"] == actual
    assert len(listed) == manifest["counts"]["wordings"] + 4


# -- the command line ------------------------------------------------------------------
def refuse_to_write(*args, **kwargs):
    raise AssertionError("the generator must not write when it refuses")


def test_a_non_default_seed_without_an_out_folder_is_refused(monkeypatch, capsys):
    # Arrange: writing is booby-trapped, so a regression cannot touch the
    # committed golden set
    monkeypatch.setattr(cli, "write_dataset", refuse_to_write)

    # Act
    with pytest.raises(SystemExit) as refusal:
        cli.main(["--seed", str(OTHER_SEED)])

    # Assert
    assert refusal.value.code == 2
    message = capsys.readouterr().err
    assert "--out" in message
    assert str(catalogue.DEFAULT_SEED) in message


def test_the_default_seed_without_an_out_folder_targets_the_committed_folder(
    monkeypatch, synthetic_dir: Path
):
    # Arrange
    calls = []
    monkeypatch.setattr(
        cli, "write_dataset", lambda dataset, seed, out: calls.append((seed, out)) or []
    )

    # Act
    exit_code = cli.main([])

    # Assert
    assert exit_code == 0
    assert calls == [(catalogue.DEFAULT_SEED, cli.DEFAULT_OUT)]
    assert synthetic_dir == cli.DEFAULT_OUT


def test_a_non_default_seed_is_allowed_with_an_explicit_out_folder(tmp_path: Path):
    # Act
    exit_code = cli.main(["--seed", str(OTHER_SEED), "--out", str(tmp_path)])

    # Assert
    assert exit_code == 0
    assert (tmp_path / "manifest.json").is_file()


def test_a_failing_generator_run_fails_the_test_with_its_stderr(
    tmp_path: Path, run_generator
):
    # Arrange: an output "folder" that is a file, so the generator crashes
    blocked = tmp_path / "blocked"
    blocked.write_text("in the way")

    # Act
    with pytest.raises(pytest.fail.Exception) as failure:
        run_generator(blocked)

    # Assert
    message = str(failure.value)
    assert "generator exited with" in message
    assert "Traceback" in message
